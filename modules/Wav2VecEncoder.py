from argparse import Namespace
import contextlib
import copy
import math
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from dataclasses import dataclass, field
from omegaconf import MISSING, II, open_dict
from typing import Any, Optional

from fairseq import checkpoint_utils, tasks, utils
from fairseq.dataclass import FairseqDataclass
from fairseq.dataclass.utils import convert_namespace_to_omegaconf
from fairseq.tasks import FairseqTask
from fairseq.models import (
    BaseFairseqModel,
    FairseqEncoder,
    FairseqEncoderDecoderModel,
    FairseqIncrementalDecoder,
    register_model,
)
from fairseq.models.wav2vec.wav2vec2 import MASKING_DISTRIBUTION_CHOICES
from fairseq.modules import (
    LayerNorm,
    PositionalEmbedding,
    TransformerDecoderLayer,
)

from .Wav2Vec2Model import Wav2Vec2Config, Wav2Vec2Model
from .utils_for_asr import Embedding, Linear, convert_dict_to_config

@dataclass
class Wav2Vec2AsrConfig(FairseqDataclass):
    # Parameter settings for fine-tuning
    w2v_path: str = field(  #
        default=MISSING, metadata={"help": "path to wav2vec 2.0 model"}
    )
    no_pretrained_weights: bool = field(  #
        default=False, metadata={"help": "if true, does not load pretrained weights"}
    )
    dropout_input: float = field(
        default=0.0,
        metadata={"help": "dropout to apply to the input (after feat extr)"},
    )
    final_dropout: float = field(  #
        default=0.0,
        metadata={"help": "dropout after transformer and before final projection"},
    )
    dropout: float = field(  
        default=0.0, metadata={"help": "dropout probability inside transformer of wav2vec 2.0 model"}
    )
    attention_dropout: float = field(
        default=0.0,
        metadata={
            "help": "dropout probability for attention weights inside transformer of wav2vec 2.0 model"
        },
    )
    activation_dropout: float = field(
        default=0.0,
        metadata={
            "help": "dropout probability after activation in FFN inside wav2vec 2.0 model"
        },
    )
    conv_feature_layers: Optional[str] = field(
        default="[(512, 10, 5)] + [(512, 3, 2)] * 4 + [(512,2,2)] + [(512,2,2)]",
        metadata={
            "help": (
                "string describing convolutional feature extraction "
                "layers in form of a python list that contains "
                "[(dim, kernel_size, stride), ...]"
            ),
        },
    )
    encoder_embed_dim: Optional[int] = field(
        default=768, metadata={"help": "encoder embedding dimension"}
    )
    feature_grad_mult: float = field(
        default=0.0, metadata={"help": "reset feature grad mult in wav2vec 2.0 to this"}
    )
    layerdrop: float = field(
        default=0.0, metadata={"help": "probability of dropping a layer in wav2vec 2.0"}
    )
    # time axis masking
    apply_mask: bool = field(  #
        default=False, metadata={"help": "apply masking during fine-tuning"}
    )
    mask_length: int = field(
        default=10, metadata={"help": "repeat the mask indices multiple times"}
    )
    mask_prob: float = field(
        default=0.5,
        metadata={
            "help": "probability of replacing a token with mask (normalized by length)"
        },
    )
    mask_selection: MASKING_DISTRIBUTION_CHOICES = field(
        default="static", metadata={"help": "how to choose masks"}
    )
    mask_other: float = field(
        default=0,
        metadata={
            "help": "secondary mask argument (used for more complex distributions), "
            "see help in compute_mask_indices"
        },
    )
    no_mask_overlap: bool = field(
        default=False, metadata={"help": "whether to allow masks to overlap"}
    )
    mask_min_space: Optional[int] = field(
        default=1,
        metadata={"help": "min space between spans (if no overlap is enabled)"},
    )

    # channel axis masking
    mask_channel_length: int = field(
        default=10, metadata={"help": "length of the mask for features (channels)"}
    )
    mask_channel_prob: float = field(
        default=0.0, metadata={"help": "probability of replacing a feature with 0"}
    )
    mask_channel_selection: MASKING_DISTRIBUTION_CHOICES = field(
        default="static",
        metadata={"help": "how to choose mask length for channel masking"},
    )
    mask_channel_other: float = field(
        default=0,
        metadata={
            "help": "secondary mask argument (used for more complex distributions), "
            "see help in compute_mask_indicesh"
        },
    )
    no_mask_channel_overlap: bool = field(
        default=False, metadata={"help": "whether to allow channel masks to overlap"}
    )
    mask_channel_min_space: Optional[int] = field(
        default=1,
        metadata={"help": "min space between spans (if no overlap is enabled)"},
    )
    mask_channel_before: bool = False

    # Fine-tuning related
    freeze_finetune_updates: int = field(  #
        default=0, metadata={"help": "dont finetune wav2vec for this many updates"}
    )
    normalize: bool = II("task.normalize")
    data: str = II("task.data")
    # this holds the loaded wav2vec args
    w2v_args: Any = None


class Wav2VecEncoder(FairseqEncoder):
    def __init__(self, cfg: Wav2Vec2AsrConfig, output_size=None):
        self.apply_mask = cfg.apply_mask

        arg_overrides = {
            "dropout": cfg.dropout,
            "activation_dropout": cfg.activation_dropout,
            "dropout_input": cfg.dropout_input,
            "attention_dropout": cfg.attention_dropout,
            "mask_length": cfg.mask_length,
            "mask_prob": cfg.mask_prob,
            "mask_selection": cfg.mask_selection,
            "mask_other": cfg.mask_other,
            "no_mask_overlap": cfg.no_mask_overlap,
            "mask_channel_length": cfg.mask_channel_length,
            "mask_channel_prob": cfg.mask_channel_prob,
            "mask_channel_before": cfg.mask_channel_before,
            "mask_channel_selection": cfg.mask_channel_selection,
            "mask_channel_other": cfg.mask_channel_other,
            "no_mask_channel_overlap": cfg.no_mask_channel_overlap,
            "encoder_layerdrop": cfg.layerdrop,
            "feature_grad_mult": cfg.feature_grad_mult,
        }
        
        if cfg.w2v_args is None:
            state = checkpoint_utils.load_checkpoint_to_cpu(cfg.w2v_path, arg_overrides)
            # Get the config of loaed w2v model
            w2v_args = state.get("cfg", None)
            if w2v_args is None:
                w2v_args = convert_namespace_to_omegaconf(state["args"])
            w2v_args.criterion = None
            w2v_args.lr_scheduler = None
            cfg.w2v_args = w2v_args 
        else:
            state = None
            w2v_args = cfg.w2v_args
            if isinstance(w2v_args, Namespace):
                cfg.w2v_args = w2v_args = convert_namespace_to_omegaconf(w2v_args)
    
        # w2v_args.task -> Config for pre-training
        # cfg -> Config for fine-tuning
        assert cfg.normalize == w2v_args.task.normalize, (
            "Fine-tuning works best when data normalization is the same. "
            "Please check that --normalize is set or unset for both pre-training and here"
        )
        # Here, data for fine-tuning maybe...
        w2v_args.task.data = cfg.data
        
        # Does not support for loading fine-tuned parameters yet
        if w2v_args.model._name == 'wav2vec_ctc':
            w2v_config = w2v_args.model.w2v_args.model
        elif w2v_args.model._name == 'wav2vec2':
            w2v_config = w2v_args.model
        else:
            w2v_config = None
        
        w2v_config = convert_dict_to_config(w2v_config)
        task = tasks.setup_task(w2v_args.task)
        model = Wav2Vec2Model(w2v_config)
             
        if state is not None and not cfg.no_pretrained_weights:
            model.load_state_dict(state["model"], strict=True)

        model.remove_pretraining_modules()

        super().__init__(task.source_dictionary)

        d = w2v_args.model.encoder_embed_dim

        self.w2v_model = model

        self.final_dropout = nn.Dropout(cfg.final_dropout)
        self.freeze_finetune_updates = cfg.freeze_finetune_updates
        self.num_updates = 0

        targ_d = None
        self.proj = None

        if output_size is not None:
            targ_d = output_size
        elif getattr(cfg, "decoder_embed_dim", d) != d:
            targ_d = cfg.decoder_embed_dim

        if targ_d is not None:
            self.proj = Linear(d, targ_d)

    def set_num_updates(self, num_updates):
        """Set the number of parameters updates."""
        super().set_num_updates(num_updates)
        self.num_updates = num_updates

    def forward(self, source, padding_mask, **kwargs):

        w2v_args = {
            "source": source,
            "padding_mask": padding_mask,
            "mask": self.apply_mask and self.training,
        }

        ft = self.freeze_finetune_updates <= self.num_updates

        with torch.no_grad() if not ft else contextlib.ExitStack():
            res = self.w2v_model.extract_features(**w2v_args)

            x = res["x"]
            padding_mask = res["padding_mask"]

            # B x T x C -> T x B x C
            x = x.transpose(0, 1)

        x = self.final_dropout(x)

        if self.proj:
            x = self.proj(x)

        return {
            "encoder_out": x,  # T x B x C
            "padding_mask": padding_mask,  # B x T,
            "layer_results": res["layer_results"],
        }

    def forward_torchscript(self, net_input):
        if torch.jit.is_scripting():
            return self.forward(net_input["source"], net_input["padding_mask"])
        else:
            return self.forward_non_torchscript(net_input)

    def reorder_encoder_out(self, encoder_out, new_order):
        if encoder_out["encoder_out"] is not None:
            encoder_out["encoder_out"] = encoder_out["encoder_out"].index_select(
                1, new_order
            )
        if encoder_out["padding_mask"] is not None:
            encoder_out["padding_mask"] = encoder_out[
                "padding_mask"
            ].index_select(0, new_order)
        return encoder_out