import argparse
import itertools
import logging
import math

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from .metrics import calc_all, calc_batch_mi
from pytorch_lightning import LightningModule
from torch import optim
from transformers import (
    AdamW,
    T5Config,
    T5ForConditionalGeneration,
    get_linear_schedule_with_warmup,
)

from transformers.generation.stopping_criteria import (
    MaxLengthCriteria,
    StoppingCriteriaList,
)
# from .vendor_t5 import ModifiedT5ForConditionalGeneration
from .vendor_t5_ntm import ModifiedT5ForConditionalGeneration

logging.getLogger("transformers").setLevel(logging.ERROR)


class T5VAE(LightningModule):
    def __init__(
        self,
        tokenizer,
        iterations_per_training_epoch,
        latent_dim,
        pooling_strategy,
        min_z=None,
        fixed_reg_weight=None,
        denoise_percentage=0,
        base_model="t5-base",
        bow_head = None,
    ):
        super().__init__()
        self.config = T5Config.from_pretrained(base_model)
        self.t5 = ModifiedT5ForConditionalGeneration.from_pretrained(
            base_model,
            config=self.config,
            latent_dim=latent_dim,
            pooling_strategy=pooling_strategy,
            min_z = min_z,
            bow_head = bow_head
        )
        self.iterations_per_training_epoch = iterations_per_training_epoch
        self.tokenizer = tokenizer
        self.latent_dim = latent_dim
        self.decoder_unfreeze_step = None
        self.min_z = min_z
        self.fixed_reg_weight = fixed_reg_weight
        self.denoise_percentage = denoise_percentage

    def freeze_decoder(self):
        for param in self.t5.memory_projection.parameters():
            param.requires_grad = False
        for param in self.t5.decoder.parameters():
            param.requires_grad = False
        for param in self.t5.lm_head.parameters():
            param.requires_grad = False

    def forward(self, encoder_input, encoder_mask, labels, **kwargs):
        output = self.t5(
            input_ids=encoder_input,
            attention_mask=encoder_mask,
            labels=labels,
            output_hidden_states=True,
            **kwargs
        )
        return (output.logits, output.z, output.mu, output.logvar, output.bow)

    #####
    # Torch lightning
    #####

    def run_batch(self, batch, batch_idx, training=False):
        encoder_inputs, encoder_masks, decoder_targets, targets_bow = batch

        assert (not encoder_inputs.isnan().any()) and (not encoder_masks.isnan().any())

        if training and self.denoise_percentage:
            for i, (inp, msk) in enumerate(zip(encoder_inputs, encoder_masks)):
                token_length = (msk.sum() - 1).item()
                max_drop = int(token_length * self.denoise_percentage)
                if max_drop > 1:
                    drop_count = torch.randint(max_drop, size=(1,)).item()
                else:
                    drop_count = 0
                drop_index = torch.randperm(token_length)[:drop_count]
                inp = torch.tensor(
                    [t for n, t in enumerate(inp) if n not in drop_index]
                )
                msk = torch.tensor(
                    [t for n, t in enumerate(msk) if n not in drop_index]
                )
                inp = torch.cat(
                    (inp, torch.tensor([self.tokenizer.pad_token_id] * drop_count))
                )
                msk = torch.cat((msk, torch.tensor([0] * drop_count)))
                encoder_inputs[i] = inp
                encoder_masks[i] = msk

        batch_size = encoder_inputs.shape[0]

        x, z, mu, logvar, bow = self(
            encoder_inputs,
            encoder_masks,
            labels=decoder_targets,
        )

        recon_loss = self.reconstruction_loss(x, decoder_targets)
        reg_loss = self.regularization_loss(mu, logvar, training)
        bow_loss = self.bow_loss(bow, targets_bow)

        return recon_loss.mean(), reg_loss.mean(), bow_loss.mean()

    def get_loss_weights(self, ratio=(0.33, 0.66),  min_baseline=1e-3):
        '''dynamic kld weight based on epoch number
        recon weight fixed @ zero
        reg_loss gradually increases
        bow weight fixed (baseline weight)
        '''
        if self.fixed_reg_weight is not None:
            return self.fixed_reg_weight
        # cycle_size = self.iterations_per_training_epoch // n_cycle
        cycle_size = self.iterations_per_training_epoch * 100

        step = self.global_step % cycle_size
        if step / cycle_size <= ratio[0]:
            return min_baseline, min_baseline, 1

        kl_weight = min((step/cycle_size - ratio[0]) / (ratio[1] - ratio[0]), 1)

        if step / cycle_size <= ratio[1]:
            return kl_weight, min_baseline

        return min_baseline, kl_weight * 0.4, 1

    def training_step(self, batch, batch_idx):
        recon_loss, reg_loss, bow_loss = self.run_batch(batch, batch_idx, training=True)
        recon_weight, reg_weight, bow_weight = self.get_loss_weights()
        loss = recon_weight * recon_loss + reg_weight * reg_loss + bow_weight * bow_loss
        self.log("train_reg_weight", reg_weight)
        self.log("train_recon_loss", recon_loss)
        self.log("train_reg_loss", reg_weight * reg_loss)
        self.log("train_unweighted_reg_loss", reg_loss)
        self.log("train_bow_loss", bow_loss)
        self.log("train_loss", loss)
        return loss

    def on_train_epoch_end(self):
        # if self.current_epoch == 2:
        # self.decoder_unfreeze_step = self.global_step
        # for param in self.t5.decoder.parameters():
        #    param.requires_grad = True
        # for param in self.t5.lm_head.parameters():
        # param.requires_grad = True
        self.log("finished_epoch", self.current_epoch)
        return

    def validation_step(self, batch, batch_idx):
        recon_loss, reg_loss, bow_loss = self.run_batch(batch, batch_idx)
        loss = recon_loss + reg_loss + bow_loss
        # mi = calc_batch_mi(self, batch)
        self.log("val_recon_loss", recon_loss)
        self.log("val_reg_loss", reg_loss)
        self.log("val_bow_loss", bow_loss)
        self.log("val_loss", loss)
        # self.log("finished_epoch", self.current_epoch)
        return loss

    def on_validation_epoch_end(self):
        ppl, nll, elbo, rec, kl, mi, au = calc_all(self, self.val_dataloader())
        self.log("val_ppl", ppl)
        self.log("val_nll", nll)
        self.log("val_elbo", elbo)
        self.log("val_rec", rec)
        self.log("val_kl", kl)
        self.log("val_mi", mi)
        self.log("val_au", au)

    def test_step(self, batch, batch_idx):
        recon_loss, reg_loss, bow_loss = self.run_batch(batch, batch_idx)
        loss = recon_loss + reg_loss + bow_loss
        self.log("test_loss", recon_loss)
        self.log("test_reg_loss", reg_loss)
        self.log("test_bow_loss", bow_loss)
        self.log("test_loss", loss)
        self.log("finished_epoch", self.current_epoch)
        return loss

    # https://github.com/PyTorchLightning/pytorch-lightning/issues/3095

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.t5.parameters(), lr=1e-4)
        # optimizer = optim.SGD(self.t5.parameters(), lr=0.01, momentum=0.9)
        # scheduler = get_linear_schedule_with_warmup(
        #    optimizer,
        #    num_warmup_steps=5000,
        # num_warmup_steps=200,
        #    num_training_steps=130000,
        # num_training_steps=2400,
        # )
        # return [optimizer], [scheduler]
        return optimizer

    def reconstruction_loss(self, x, target):
        loss = F.cross_entropy(
            x.transpose(1, 2),
            target,
            ignore_index=self.tokenizer.pad_token_id,
            reduction="none",
        )
        return loss

    def regularization_loss(self, mu, logvar, training=False):
        return self.t5.calc_kl(mu, logvar, training)

    def bow_loss(self, x, target_bow):
        return nn.BCEWithLogitsLoss()(x, target_bow)

    def calc_mi(self, z, mu, logvar):
        return self.t5.calc_mi(z, mu, logvar)


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("-a", "--argument", help="Example argument.")
    args = parser.parse_args()
