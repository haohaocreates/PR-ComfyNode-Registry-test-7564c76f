from typing import Union
import torch
import torch.nn.functional as F
from torch import Tensor, nn

import comfy.model_management as model_management
import comfy.ops
import comfy.utils
from comfy.cli_args import args
from comfy.ldm.modules.attention import attention_basic, attention_pytorch, attention_split, attention_sub_quad, default

from .logger import logger


# until xformers bug is fixed, do not use xformers for VersatileAttention! TODO: change this when fix is out
# logic for choosing optimized_attention method taken from comfy/ldm/modules/attention.py
optimized_attention_mm = attention_basic
if model_management.xformers_enabled():
    pass
    #optimized_attention_mm = attention_xformers
if model_management.pytorch_attention_enabled():
    optimized_attention_mm = attention_pytorch
else:
    if args.use_split_cross_attention:
        optimized_attention_mm = attention_split
    else:
        optimized_attention_mm = attention_sub_quad


# maintain backwards compatibility with the comfy.ops hasattr check (TODO: remove once a non-backwards compatible change happens)
class CrossAttentionMM(nn.Module):
    def __init__(self, query_dim, context_dim=None, heads=8, dim_head=64, dropout=0., dtype=None, device=None,
                 operations=comfy.ops.disable_weight_init if hasattr(comfy.ops, "disable_weight_init") else comfy.ops):
        super().__init__()
        inner_dim = dim_head * heads
        context_dim = default(context_dim, query_dim)

        self.heads = heads
        self.dim_head = dim_head
        self.scale = None
        self.default_scale = dim_head ** -0.5

        self.to_q = operations.Linear(query_dim, inner_dim, bias=False, dtype=dtype, device=device)
        self.to_k = operations.Linear(context_dim, inner_dim, bias=False, dtype=dtype, device=device)
        self.to_v = operations.Linear(context_dim, inner_dim, bias=False, dtype=dtype, device=device)

        self.to_out = nn.Sequential(operations.Linear(inner_dim, query_dim, dtype=dtype, device=device), nn.Dropout(dropout))

    def forward(self, x, context=None, value=None, mask=None, scale_mask=None):
        q = self.to_q(x)
        context = default(context, x)
        k: Tensor = self.to_k(context)
        if value is not None:
            v = self.to_v(value)
            del value
        else:
            v = self.to_v(context)

        # apply custom scale by multiplying k by scale factor
        if self.scale is not None:
            k *= self.scale
        
        # apply scale mask, if present
        if scale_mask is not None:
            k *= scale_mask

        out = optimized_attention_mm(q, k, v, self.heads, mask)
        return self.to_out(out)

# TODO: set up comfy.ops style classes for groupnorm and other functions
class GroupNormAD(torch.nn.GroupNorm):
    def __init__(self, num_groups: int, num_channels: int, eps: float = 1e-5, affine: bool = True,
                 device=None, dtype=None) -> None:
        super().__init__(num_groups=num_groups, num_channels=num_channels, eps=eps, affine=affine, device=device, dtype=dtype)
    
    def forward(self, input: Tensor) -> Tensor:
        return F.group_norm(
             input, self.num_groups, self.weight, self.bias, self.eps)


# applies min-max normalization, from:
# https://stackoverflow.com/questions/68791508/min-max-normalization-of-a-tensor-in-pytorch
def normalize_min_max(x: Tensor, new_min = 0.0, new_max = 1.0):
    new_min = float(new_min)
    new_max = float(new_max)
    x_min, x_max = x.min(), x.max()
    return (((x - x_min)/(x_max - x_min)) * (new_max - new_min)) + new_min


# adapted from comfy/sample.py
def prepare_mask_batch(mask: Tensor, shape: Tensor, multiplier: int=1, match_dim1=False):
    mask = mask.clone()
    mask = torch.nn.functional.interpolate(mask.reshape((-1, 1, mask.shape[-2], mask.shape[-1])), size=(shape[2]*multiplier, shape[3]*multiplier), mode="bilinear")
    if match_dim1:
        mask = torch.cat([mask] * shape[1], dim=1)
    return mask


def extend_to_batch_size(tensor: Tensor, batch_size: int):
    if tensor.shape[0] > batch_size:
        return tensor[:batch_size]
    elif tensor.shape[0] < batch_size:
        remainder = batch_size-tensor.shape[0]
        return torch.cat([tensor] + [tensor[-1:]]*remainder, dim=0)
    return tensor


class MotionCompatibilityError(ValueError):
    pass


def get_combined_multival(multivalA: Union[float, Tensor], multivalB: Union[float, Tensor]) -> Union[float, Tensor]:
    # if one is None, use the other
    if multivalA == None:
        return multivalB
    elif multivalB == None:
        return multivalA 
    # both have a value - combine them based on type
    # if both are Tensors, make dims match before multiplying
    if type(multivalA) == Tensor and type(multivalB) == Tensor:
        areaA = multivalA.shape[1]*multivalA.shape[2]
        areaB = multivalB.shape[1]*multivalB.shape[2]
        # match height/width to mask with larger area
        leader,follower = multivalA,multivalB if areaA >= areaB else multivalB,multivalA
        batch_size = multivalA.shape[0] if multivalA.shape[0] >= multivalB.shape[0] else multivalB.shape[0]
        # make follower same dimensions as leader
        follower = torch.unsqueeze(follower, 1)
        follower = comfy.utils.common_upscale(follower, leader.shape[2], leader.shape[1], "bilinear", "center")
        follower = torch.squeeze(follower, 1)
        # make sure batch size will match
        leader = extend_to_batch_size(leader, batch_size)
        follower = extend_to_batch_size(follower, batch_size)
        return leader * follower
    # otherwise, just multiply them together - one of them is a float
    return multivalA * multivalB


class ADKeyframe:
    def __init__(self,
                 start_percent: float = 0.0,
                 scale_multival: Union[float, Tensor]=None,
                 effect_multival: Union[float, Tensor]=None,
                 inherit_missing: bool=True,
                 guarantee_usage: bool=True,
                 ):
        self.start_percent = start_percent
        self.start_t = 999999999.9
        self.scale_multival = scale_multival
        self.effect_multival = effect_multival
        self.inherit_missing = inherit_missing
        self.guarantee_usage = guarantee_usage
    
    def has_scale(self):
        return self.scale_multival is not None
    
    def has_effect(self):
        return self.effect_multival is not None


class ADKeygrameGroup:
    def __init__(self):
        self.keyframes: list[ADKeyframe] = []
        self.keyframes.append(ADKeyframe())
    
    def add(self, keyframe: ADKeyframe):
        added = False
        # replace existing keyframe if same start_percent
        for i in range(len(self.keyframes)):
            if self.keyframes[i].start_percent == keyframe.start_percent:
                self.keyframes[i] = keyframe
                added = True
                break
        if not added:
            self.keyframes.append(keyframe)
        self.keyframes.sort(key=lambda k: k.start_percent)
    
    def get_index(self, index: int) -> Union[ADKeyframe, None]:
        try:
            return self.keyframes[index]
        except IndexError:
            return None
    
    def has_index(self, index: int) -> int:
        return index >=0 and index < len(self.keyframes)

    def __getitem__(self, index) -> ADKeyframe:
        return self.keyframes[index]

    def __len__(self) -> int:
        return len(self.keyframes)

    def is_empty(self) -> bool:
        return len(self.keyframes) == 0

    def clone(self) -> 'ADKeygrameGroup':
        cloned = ADKeygrameGroup()
        for tk in self.keyframes:
            cloned.add(tk)
        return cloned
