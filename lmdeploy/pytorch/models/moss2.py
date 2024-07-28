# Copyright (c) The Moss team and The HuggingFace Inc. team. All rights reserved.
#
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" PyTorch Moss2 model."""
from typing import List, Optional, Tuple, Union
import torch
import torch.distributed as dist

import transformers
from packaging import version
from torch import nn

from einops import rearrange
from transformers.modeling_outputs import (
    BaseModelOutputWithPast,
)
from ..kernels import (apply_rotary_pos_emb,
                       fill_kv_cache, paged_attention_fwd)
from ..weight_loader.dist_utils import (colwise_parallelize_linear,
                                        rowwise_parallelize_linear)


class Moss2Attention(nn.Module):
    def _load_weights(self, loader, rank: int, world_size: int,
                      device: torch.device):
        """load weights."""
        for mod_name in ['wqkv']:
            colwise_parallelize_linear(getattr(self, mod_name),
                                       loader,
                                       rank=rank,
                                       world_size=world_size,
                                       prefix=mod_name)
        for mod_name in ['wo']:
            rowwise_parallelize_linear(getattr(self, mod_name),
                                       loader,
                                       rank=rank,
                                       world_size=world_size,
                                       prefix=mod_name)

    @classmethod
    def _distribute_output_fn(cls, outputs, **kwargs):
        """Distribution output hook."""
        dist.all_reduce(outputs[0])
        return outputs
    

    def _contiguous_batching_forward_impl(
        self,
        hidden_states: torch.Tensor,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: bool = False,
        world_size: int = 1,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor],
               Optional[Tuple[torch.Tensor]]]:
        """Rewrite implementation of LlamaAttention.forward.

        Add continuous batching support. Add paged attention support. TP
        support.
        """
        context = self.context.context
        q_start_loc = context.q_start_loc
        q_seq_length = context.q_seq_length
        kv_seq_length = context.kv_seq_length
        block_offsets = context.block_offsets
        max_q_seq_length = context.max_q_seq_length
        position_ids_1d = context.position_ids_1d
        max_kv_seq_length = context.max_kv_seq_length

        def __qkv_proj(hidden_states):
            """qkv_proj."""
            qkv_states = self.wqkv(hidden_states)
            qkv_states = rearrange(
                qkv_states,
                'b q (h gs d) -> (b q) h gs d',
                gs=2 + self.num_key_value_groups,
                d=self.head_dim,
            )
            query_states = qkv_states[..., :self.num_key_value_groups, :]
            query_states = query_states.flatten(1, 2)
            key_states = qkv_states[..., -2, :]
            value_states = qkv_states[..., -1, :]
            return query_states, key_states, value_states

        def __rotary_emb_fn(query_states, key_states, value_states):
            """rotary embedding func."""
            # compat
            if not hasattr(self, '_use_old_rotary_emb'):
                import inspect
                args = inspect.getargspec(self.rotary_emb.forward)[0]
                self._use_old_rotary_emb = 'seq_len' in args

            if not hasattr(context, '_cos'):
                if self._use_old_rotary_emb:
                    kwargs = dict(seq_len=max_kv_seq_length)
                else:
                    kwargs = dict(position_ids=position_ids_1d[None])

                cos, sin = self.rotary_emb(value_states.transpose(0, 1),
                                           **kwargs)
                context._cos = cos
                context._sin = sin
            else:
                cos = context._cos
                sin = context._sin

            if self._use_old_rotary_emb:
                _position_ids_1d = position_ids_1d
            else:
                _position_ids_1d = torch.arange(0,
                                                len(position_ids_1d),
                                                device=query_states.device)
            query_states, key_states = apply_rotary_pos_emb(
                query_states,
                key_states,
                cos,
                sin,
                position_ids,
                position_ids_1d=_position_ids_1d,
                q_embed=query_states,
                k_embed=key_states)
            return query_states, key_states, value_states

        query_states, key_states, value_states = __qkv_proj(hidden_states)

        query_states, key_states, value_states = __rotary_emb_fn(
            query_states, key_states, value_states)

        fill_kv_cache(
            key_states,
            value_states,
            past_key_value[0],
            past_key_value[1],
            q_start_loc,
            q_seq_length,
            kv_seq_length=kv_seq_length,
            max_q_seq_length=max_q_seq_length,
            block_offsets=block_offsets,
        )

        attn_output = query_states
        paged_attention_fwd(
            query_states,
            past_key_value[0],
            past_key_value[1],
            attn_output,
            block_offsets,
            q_start_loc=q_start_loc,
            q_seqlens=q_seq_length,
            kv_seqlens=kv_seq_length,
            max_seqlen=max_q_seq_length,
        )
        attn_output = attn_output.reshape(*hidden_states.shape[:-1], -1)

        attn_output = self.wo(attn_output)

        return attn_output, None, past_key_value

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
        output_attentions: bool = False,
        **kwargs
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor],
               Optional[Tuple[torch.Tensor]]]:
        """forward."""
        world_size = 1
        if dist.is_initialized():
            world_size = dist.get_world_size()
        return self._contiguous_batching_forward_impl(
            hidden_states,
            position_ids,
            past_key_value,
            output_attentions,
            world_size=world_size,
        )

class Moss2MLP(nn.Module):

    def _load_weights(self, loader, rank: int, world_size: int,
                      device: torch.device):
        """load weights."""
        for mod_name in ['w1', 'w3']:
            colwise_parallelize_linear(getattr(self, mod_name),
                                       loader,
                                       rank=rank,
                                       world_size=world_size,
                                       prefix=mod_name)
        for mod_name in ['w2']:
            rowwise_parallelize_linear(getattr(self, mod_name),
                                       loader,
                                       rank=rank,
                                       world_size=world_size,
                                       prefix=mod_name)

    @classmethod
    def _distribute_output_fn(cls, outputs, **kwargs):
        """Distribution output hook."""
        dist.all_reduce(outputs)
        return outputs


class Moss2Model(nn.Module):

    def _continuous_batching_forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
    ) -> Union[Tuple, BaseModelOutputWithPast]:
        """Rewrite implementation of LlamaModel.forward."""
        context = self.context.context
        # get inputs from context
        vision_embeddings = context.input_embeddings
        vision_embedding_indexing = context.input_embedding_indexing

        if inputs_embeds is None:
            inputs_embeds = self.tok_embeddings(input_ids)

        if vision_embeddings is not None and len(vision_embeddings) > 0:
            inputs_embeds[:,
                          vision_embedding_indexing, :] = vision_embeddings.to(
                              inputs_embeds)

        # Attention mask is not necessary in continuous batching
        attention_mask = None
        hidden_states = inputs_embeds

        # decoder layers
        for idx, decoder_layer in enumerate(self.layers):
            past_key_value = (past_key_values[idx]
                              if past_key_values is not None else None)
            layer_outputs = decoder_layer(
                hidden_states,
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_value=past_key_value,
                output_attentions=output_attentions,
                use_cache=use_cache,
            )
            hidden_states = layer_outputs[0]

        hidden_states = self.norm(hidden_states)

        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states,
            past_key_values=past_key_value,
            hidden_states=None,
            attentions=None,
        )

    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[List[torch.FloatTensor]] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        **kwargs,
    ) -> Union[Tuple, BaseModelOutputWithPast]:
        """Rewrite of LlamaModel.forward."""
        return self._continuous_batching_forward(
            input_ids,
            attention_mask,
            position_ids,
            past_key_values,
            inputs_embeds,
            use_cache,
            output_attentions,
        )
