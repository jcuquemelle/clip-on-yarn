import os
import time
import logging

import wandb
import torch
import torch.nn as nn
from torch.cuda.amp import autocast
import torch.distributed as dist
import torch.distributed.nn
from tf_yarn.pytorch import model_ckpt


logger = logging.getLogger()


def model_inference(model, images, texts):
    image_features = model.encode_image(images)
    text_features = model.encode_text(texts)
    image_features = image_features / image_features.norm(dim=-1, keepdim=True)
    text_features = text_features / text_features.norm(dim=-1, keepdim=True)
    return image_features, text_features, model.logit_scale.exp()


def get_loss(model, images, texts, loss_img, loss_txt, aggregate, device, sharded_loss= True):
    image_features, text_features, logit_scale = model_inference(model.module, images, texts)
    logit_scale = logit_scale.mean()
    rank = dist.get_rank()
    if aggregate and not sharded_loss:
        world_size = dist.get_world_size()

        # We gather tensors from all gpus to get more negatives to contrast with.
        gathered_image_features = [
            torch.zeros_like(image_features) for _ in range(world_size)
        ]
        gathered_text_features = [
            torch.zeros_like(text_features) for _ in range(world_size)
        ]
        dist.all_gather(gathered_image_features, image_features)
        dist.all_gather(gathered_text_features, text_features)

        all_image_features = torch.cat(
            [image_features]
            + gathered_image_features[:rank]
            + gathered_image_features[rank + 1 :]
        )
        all_text_features = torch.cat(
            [text_features]
            + gathered_text_features[:rank]
            + gathered_text_features[rank + 1 :]
        )

        # this is needed to send gradients back everywhere.
        logits_per_image = logit_scale * all_image_features @ all_text_features.t()
        logits_per_text = logits_per_image.t()

    elif aggregate and sharded_loss:
         all_image_features = torch.cat(dist.nn.all_gather(image_features))
         all_text_features = torch.cat(dist.nn.all_gather(text_features))
         logits_per_image = logit_scale * image_features @ all_text_features.t()
         logits_per_text = logit_scale * text_features @ all_image_features.t()
         if torch.isnan(image_features).any():
             torch.set_printoptions(profile="full")
             print(image_features)
             raise ValueError("NaN detected in local image embeddings !!!")
         if torch.isnan(text_features).any():
             torch.set_printoptions(profile="full")
             print(text_features)
             raise ValueError("NaN detected in local text embeddings")
         if torch.isnan(all_image_features).any():
             torch.set_printoptions(profile="full")
             print(all_image_features)
             raise ValueError("NaN detected in gathered image embeddings !!!")
         if torch.isnan(all_text_features).any():
             torch.set_printoptions(profile="full")
             print(all_text_features)
             raise ValueError("NaN detected in gathered text embeddings !!!")
         if torch.isnan(logits_per_image).any() or torch.isnan(logits_per_text).any():
             raise ValueError("NaN detected in logits !!!")
    else:
        logits_per_image = logit_scale * image_features @ text_features.t()
        logits_per_text = logit_scale * text_features @ image_features.t()

    ground_truth = torch.arange(len(logits_per_image)).long()
    ground_truth = ground_truth.to(device, non_blocking=True)
    total_loss = (
        loss_img(logits_per_image, ground_truth)
        + loss_txt(logits_per_text, ground_truth)
    ) / 2
    return total_loss


def train(
    model, trainloader, epoch, optimizer, scaler, scheduler, device,
    precision, aggregate, model_save_ckpt_dir, n_steps_ckpt, tb_writer, enable_wandb, profiler
):
    model.train()
    loss_img = nn.CrossEntropyLoss().to(device)
    loss_txt = nn.CrossEntropyLoss().to(device)

    rank = dist.get_rank()
    world_size = dist.get_world_size()
    
    n_batches_per_epoch = len(trainloader)
    n_samples_per_epoch = len(trainloader.dataset) * world_size
    n_done_steps = n_batches_per_epoch * epoch

    logging_n_steps = 50
    for i in range(n_batches_per_epoch):
    #for i, batch in enumerate(trainloader):
        current_step = n_done_steps +  i
        scheduler(current_step)

        optimizer.zero_grad()

        #images = batch['image_tensor']
        #texts = batch['text_tokens']
        images = torch.rand([32, 3, 224, 224]).to(device, non_blocking=True) #images.to(device, non_blocking=True)
        texts = torch.randint(1, 100, size=[32, 77]).to(device, non_blocking=True) #texts.to(device, non_blocking=True)

        batch_size = images.shape[0]

        total_loss = get_loss(model, images, texts, loss_img, loss_txt, aggregate, device)
        total_loss.backward()
        optimizer.step()

        # Note: we clamp to 4.6052 = ln(100), as in the original paper.
        model.module.logit_scale.data = torch.clamp(model.module.logit_scale.data, 0, 4.6052)

        if (i % logging_n_steps) == 0:
            os.system("nvidia-smi")
            logger.info(f"memory_allocated: {torch.cuda.memory_allocated()}")
            logger.info(f"max_memory_allocated: {torch.cuda.max_memory_allocated()}")
            logger.info(f"memory_reserved: {torch.cuda.memory_reserved()}")
            logger.info(f"max_memory_reserved: {torch.cuda.max_memory_reserved()}")

            num_samples = i * batch_size * world_size
            percent_complete = 100.0 * i / n_batches_per_epoch
            logger.info(
                f"[{os.getpid()}] Train Epoch: {epoch} [{num_samples}/{n_samples_per_epoch} ({percent_complete:.0f}%)]\t"
                f"Loss: {total_loss.item():.6f}"
                f"\tLR: {optimizer.param_groups[0]['lr']:5f}\tlogit_scale {model.module.logit_scale.data:.3f}"
            )
            del num_samples
        del batch_size
        del images
        del texts

