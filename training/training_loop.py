﻿# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# NVIDIA CORPORATION and its licensors retain all intellectual property
# and proprietary rights in and to this software, related documentation
# and any modifications thereto.  Any use, reproduction, disclosure or
# distribution of this software and related documentation without an express
# license agreement from NVIDIA CORPORATION is strictly prohibited.

"""Main training loop."""

import os
import pickle
import time
import PIL.Image
import numpy as np
import tensorflow as tf
import dnnlib
import dnnlib.tflib as tflib
from dnnlib.tflib.autosummary import autosummary
from itertools import chain
from collections import OrderedDict

from training import dataset


# ----------------------------------------------------------------------------
# Select size and contents of the image snapshot grids that are exported
# periodically during training.

# no change
def setup_snapshot_image_grid(training_set):
    gw = np.clip(7680 // training_set.shape[2], 7, 32)
    gh = np.clip(4320 // training_set.shape[1], 4, 32)

    # Unconditional.
    if training_set.label_size == 0:
        reals, labels = training_set.get_minibatch_np(gw * gh)
        return (gw, gh), reals, labels

    # Row per class.
    cw, ch = (gw, 1)
    nw = (gw - 1) // cw + 1
    nh = (gh - 1) // ch + 1

    # Collect images.
    blocks = [[] for _i in range(nw * nh)]
    for _iter in range(1000000):
        real, label = training_set.get_minibatch_np(1)
        idx = np.argmax(label[0])
        while idx < len(blocks) and len(blocks[idx]) >= cw * ch:
            idx += training_set.label_size
        if idx < len(blocks):
            blocks[idx].append((real, label))
            if all(len(block) >= cw * ch for block in blocks):
                break

    # Layout grid.
    reals = np.zeros([gw * gh] + training_set.shape, dtype=training_set.dtype)
    labels = np.zeros([gw * gh, training_set.label_size], dtype=training_set.label_dtype)
    for i, block in enumerate(blocks):
        for j, (real, label) in enumerate(block):
            x = (i % nw) * cw + j % cw
            y = (i // nw) * ch + j // cw
            if x < gw and y < gh:
                reals[x + y * gw] = real[0]
                labels[x + y * gw] = label[0]
    return (gw, gh), reals, labels


# ----------------------------------------------------------------------------

# no change
def save_image_grid(images, filename, drange, grid_size):
    lo, hi = drange
    gw, gh = grid_size
    images = np.asarray(images, dtype=np.float32)
    images = (images - lo) * (255 / (hi - lo))
    images = np.rint(images).clip(0, 255).astype(np.uint8)
    _N, C, H, W = images.shape
    images = images.reshape(gh, gw, C, H, W)
    images = images.transpose(0, 3, 1, 4, 2)
    images = images.reshape(gh * H, gw * W, C)
    PIL.Image.fromarray(images, {3: 'RGB', 1: 'L'}[C]).save(filename)


# ----------------------------------------------------------------------------
# Main training script.

def training_loop(
        run_dir='.',  # Output directory.
        G_args={},  # Options for generator network.
        D_args={},  # Options for discriminator network.
        G_E_opt_args={},  # Options for generator optimizer.
        D_opt_args={},  # Options for discriminator optimizer.
        E_args = {},
        D_H_args={},
        D_J_args = {},
        loss_args={},  # Options for loss function.
        train_dataset_args={},  # Options for dataset to train with.
        metric_dataset_args={},  # Options for dataset to evaluate metrics against.
        augment_args={},  # Options for adaptive augmentations.
        metric_arg_list=[],  # Metrics to evaluate during training.
        num_gpus=1,  # Number of GPUs to use.
        minibatch_size=32,  # Global minibatch size.
        minibatch_gpu=4,  # Number of samples processed at a time by one GPU.
        G_smoothing_kimg=10,  # Half-life of the exponential moving average (EMA) of generator weights.
        G_smoothing_rampup=None,  # EMA ramp-up coefficient.
        minibatch_repeats=4,  # Number of minibatches to run in the inner loop.
        lazy_regularization=True,  # Perform regularization as a separate training step?
        G_reg_interval=4,  # How often the perform regularization for G? Ignored if lazy_regularization=False.
        D_reg_interval=16,  # How often the perform regularization for D? Ignored if lazy_regularization=False.
        E_reg_interval = 4, # Tabriz: it should be same with G, however D is 2x of G & E in bigbigan repo
        total_kimg=25000,  # Total length of the training, measured in thousands of real images.
        kimg_per_tick=4,  # Progress snapshot interval.
        image_snapshot_ticks=50,  # How often to save image snapshots? None = only save 'reals.png' and 'fakes-init.png'.
        network_snapshot_ticks=50,  # How often to save network snapshots? None = only save 'networks-final.pkl'.
        resume_pkl=None,  # Network pickle to resume training from.
        abort_fn=None,  # Callback function for determining whether to abort training.
        progress_fn=None,  # Callback function for updating training progress.
):
    assert minibatch_size % (num_gpus * minibatch_gpu) == 0
    start_time = time.time()

    print('Loading training set...')
    training_set = dataset.load_dataset(**train_dataset_args)
    print('Image shape:', np.int32(training_set.shape).tolist())
    print('Label shape:', [training_set.label_size])
    print()

    print('Constructing networks...')
    with tf.device('/gpu:0'):
        G = tflib.Network('G', num_channels=training_set.shape[0], resolution=training_set.shape[1],
                          label_size=training_set.label_size, **G_args) # LAST ARGUMENT IS ALL
        D = tflib.Network('D', num_channels=training_set.shape[0], resolution=training_set.shape[1],
                          label_size=training_set.label_size, **D_args)
        Gs = G.clone('Gs')

        E = tflib.Network('E', num_channels = training_set.shape[0], resolution = training_set.shape[1],
                          label_size = training_set.label_size, **E_args)
        D_H = tflib.Network('D_H', num_channels = training_set.shape[0], resolution = training_set.shape[1],
                            label_size = training_set.label_size, **D_H_args)

        D_J = tflib.Network('D_J', input_size = 256,
                            label_size = training_set.label_size, **D_J_args)

        # E.print_layers()

        # Tabriz: ENCODER DECLARATION
        if resume_pkl is not None:
            print(f'Resuming from "{resume_pkl}"')
            with dnnlib.util.open_url(resume_pkl) as f:
                rG, rD, rGs, rE, rD_H, rD_J = pickle.load(f) # Tabriz: How to load encoder?
            G.copy_vars_from(rG)
            D.copy_vars_from(rD)
            Gs.copy_vars_from(rGs)
            E.copy_vars_from(rE)
            D_H.copy_vars_from(rD_H)
            D_J.copy_vars_from(rD_J)
            # ADD ENCODER HERE

    G.print_layers()
    D.print_layers()

    E.print_layers()
    D_H.print_layers()
    D_J.print_layers()

    #physical_devices = tf.config.list_physical_devices('GPU')
    #print("Num GPUs:", len(physical_devices))
    print(tf.test.is_gpu_available())
    print('Exporting sample images...')

    grid_size, grid_reals, grid_labels = setup_snapshot_image_grid(training_set)
    save_image_grid(grid_reals, os.path.join(run_dir, 'reals.png'), drange=[0, 255], grid_size=grid_size)
    grid_latents = np.random.randn(np.prod(grid_size), *G.input_shape[1:])
    grid_fakes = Gs.run(grid_latents, grid_labels, is_validation=True, minibatch_size=minibatch_gpu)[0]
    save_image_grid(grid_fakes, os.path.join(run_dir, 'fakes_init.png'), drange=[-1, 1], grid_size=grid_size)


# Tabriz: no change

    print(f'Replicating networks across {num_gpus} GPUs...')
    G_gpus = [G]
    D_gpus = [D]
    E_gpus = [E]
    D_H_gpus = [D_H]
    D_J_gpus = [D_J]
    for gpu in range(1, num_gpus):
        with tf.device(f'/gpu:{gpu}'):
            G_gpus.append(G.clone(f'{G.name}_gpu{gpu}'))
            D_gpus.append(D.clone(f'{D.name}_gpu{gpu}'))
            D_H_gpus.append(D_H.clone(f'{D_H.name}_gpu{gpu}'))
            E_gpus.append(E.clone(f'{E.name}_gpu{gpu}'))
            D_J_gpus.append(D_J.clone(f'{D_J.name}_gpu{gpu}'))


    print('Initializing augmentations...')  # Tabriz: used only in dicriminator
    aug = None
    if augment_args.get('class_name', None) is not None:
        aug = dnnlib.util.construct_class_by_name(**augment_args)
        aug.init_validation_set(D_gpus=D_gpus, training_set=training_set)

    print('Setting up optimizers...')
    G_E_opt_args = dict(G_E_opt_args)
    D_opt_args = dict(D_opt_args)

    # Tabriz: ADD Encoder arguments here
    # Tabriz: I chose E_reg_interval equal to the G_reg_interval by intuition, but it can still need to be optimized


    # Tabriz: May or may not be needed for D_H, D_J

    for args, reg_interval in [(G_E_opt_args, G_reg_interval), (D_opt_args, D_reg_interval),]: #(E_opt_args, E_reg_interval)]:
        args['minibatch_multiplier'] = minibatch_size // num_gpus // minibatch_gpu
        if lazy_regularization:
            mb_ratio = reg_interval / (reg_interval + 1)
            args['learning_rate'] *= mb_ratio
            if 'beta1' in args: args['beta1'] **= mb_ratio
            if 'beta2' in args: args['beta2'] **= mb_ratio


    # Tabriz: Optimizer initiation, add here encoder

    G_E_opt = tflib.Optimizer(name='TrainG', **G_E_opt_args) # Adam Optimizer
    D_opt = tflib.Optimizer(name='TrainD', **D_opt_args)
     # Tabriz: Needs hyperparameter optimization


    # Tabriz: Lazy regularization
    G_reg_opt = tflib.Optimizer(name='RegG', share=G_E_opt, **G_E_opt_args)
    D_reg_opt = tflib.Optimizer(name='RegD', share=D_opt, **D_opt_args)
   # E_reg_opt = tflib.Optimizer(name='RegE', share=E_opt, **E_opt_args)
   # D_J_reg_opt = tflib.Optimizer(name='RegD_J', share = D_J_opt, **D_J_opt_args)
   # D_H_reg_opt = tflib.Optimizer(name='RegD_H', share = D_H_opt, **D_H_opt_args)


    print(training_set.shape)
    print([minibatch_gpu]+ training_set.shape)
    print('Constructing training graph...')
    data_fetch_ops = []
    training_set.configure(minibatch_gpu)
    for gpu, (G_gpu, D_gpu, E_gpu, D_H_gpu, D_J_gpu) in enumerate(zip(G_gpus, D_gpus, E_gpus, D_H_gpus, D_J_gpus)):
        with tf.name_scope(f'Train_gpu{gpu}'), tf.device(f'/gpu:{gpu}'):
            # Fetch training data via temporary variables.
            with tf.name_scope('DataFetch'):
                real_images_var = tf.Variable(name='images', trainable=False,
                                              initial_value=tf.zeros([minibatch_gpu] + training_set.shape)) # Tabriz: [32, 3, 32, 32]
                real_labels_var = tf.Variable(name='labels', trainable=False,
                                              initial_value=tf.zeros([minibatch_gpu, training_set.label_size]))
                real_images_write, real_labels_write = training_set.get_minibatch_tf()
                real_images_write = tflib.convert_images_from_uint8(real_images_write)
                data_fetch_ops += [tf.assign(real_images_var, real_images_write)]
                data_fetch_ops += [tf.assign(real_labels_var, real_labels_write)]

            # Evaluate loss function and register gradients.
            fake_labels = training_set.get_random_labels_tf(minibatch_gpu)
            terms = dnnlib.util.call_func_by_name(G=G_gpu, D=D_gpu, E = E_gpu, D_H = D_H_gpu, D_J = D_J_gpu,
                                                  aug=aug, fake_labels=fake_labels,              # Tabriz: calls loss function
                                                  real_images=real_images_var, real_labels=real_labels_var, **loss_args)

            if lazy_regularization:

                if terms.G_reg is not None: G_reg_opt.register_gradients(tf.reduce_mean(terms.G_reg * G_reg_interval),
                                                                         G_gpu.trainables)
                """Tabriz: Register the gradients of the given loss function with respect to the given variables.
                    Intended to be called once per GPU."""
                if terms.D_reg is not None: D_reg_opt.register_gradients(tf.reduce_mean(terms.D_reg * D_reg_interval),
                                                                         D_gpu.trainables)
                # Tabriz: I can add here D_J, D_H

               # if terms.E_reg is not None: E_reg_opt.register_gradients(tf.reduce_mean(terms.E_reg * E_reg_interval),
                                                                    #     E_gpu.trainables)
            else:
                if terms.G_reg is not None: terms.G_E_loss += terms.G_reg
                if terms.D_reg is not None: terms.D_loss += terms.D_reg
                if terms.E_reg is not None: terms.G_E_loss += terms.E_reg

            # assert terms.D_loss == tf.reduce_mean(terms.D_loss)

            G_E_opt.register_gradients(terms.G_E_loss, OrderedDict(chain(G_gpu.trainables.items(),
                                                                       E_gpu.trainables.items())))
            # Tabriz: Can be wrong
            # G_opt.register_gradients(tf.reduce_mean(terms.G_E_loss), G_gpu.trainables)

            D_opt.register_gradients(terms.D_loss, OrderedDict(chain(D_gpu.trainables.items(),
                                                                     D_H_gpu.trainables.items(),
                                                                     D_J_gpu.trainables.items())))
                                                                        # Tabriz: D_loss constitutes both D_J & D_H
                                                                        # but need to include also the trainable variables
                                                                        # of D_J & D_H
            # Tabriz: In repo, G and E updated together

    print('Finalizing training ops...')

    # Tabriz: I do not know if I need to add/edit smth here
    data_fetch_op = tf.group(*data_fetch_ops)
    G_train_op = G_E_opt.apply_updates()  # Tabriz: Optimizer of generator & encoder is same
    D_train_op = D_opt.apply_updates()
    G_reg_op = G_reg_opt.apply_updates(allow_no_op=True)
    D_reg_op = D_reg_opt.apply_updates(allow_no_op=True)
    Gs_beta_in = tf.placeholder(tf.float32, name='Gs_beta_in', shape=[])
    Gs_update_op = Gs.setup_as_moving_average_of(G, beta=Gs_beta_in)
    tflib.init_uninitialized_vars()
    with tf.device('/gpu:0'):
        peak_gpu_mem_op = tf.contrib.memory_stats.MaxBytesInUse()

    print('Initializing metrics...')
    summary_log = tf.summary.FileWriter(run_dir)
    metrics = []
    for args in metric_arg_list:
        metric = dnnlib.util.construct_class_by_name(**args)
        metric.configure(dataset_args=metric_dataset_args, run_dir=run_dir)
        metrics.append(metric)

    print(f'Training for {total_kimg} kimg...')
    print()
    if progress_fn is not None:
        progress_fn(0, total_kimg)
    tick_start_time = time.time()
    maintenance_time = tick_start_time - start_time
    cur_nimg = 0 # Tabriz: current number of images
    cur_tick = -1
    tick_start_nimg = cur_nimg
    running_mb_counter = 0  # Tabriz: Mini batch counter

    done = False
    while not done:  # Tabriz: training starts here

        # Compute EMA decay parameter.
        Gs_nimg = G_smoothing_kimg * 1000.0
        if G_smoothing_rampup is not None:
            Gs_nimg = min(Gs_nimg, cur_nimg * G_smoothing_rampup)
        Gs_beta = 0.5 ** (minibatch_size / max(Gs_nimg, 1e-8))

        # Run training ops.
        for _repeat_idx in range(minibatch_repeats):
            rounds = range(0, minibatch_size, minibatch_gpu * num_gpus)
            run_G_reg = (lazy_regularization and running_mb_counter % G_reg_interval == 0)
            # Tabriz: For every g_reg interval it becomes True
            run_D_reg = (lazy_regularization and running_mb_counter % D_reg_interval == 0)
            cur_nimg += minibatch_size
            running_mb_counter += 1

            # Fast path without gradient accumulation.
            if len(rounds) == 1:
                # tflib.run([D_train_op, Gs_update_op], {Gs_beta_in: Gs_beta}) # need to delete it later
                tflib.run([G_train_op, data_fetch_op]) # Tabriz: image shape was [32, 3, 3, 512, 512] some shit is going on, same with original no worries
                if run_G_reg:
                    tflib.run(G_reg_op)
                tflib.run([D_train_op, Gs_update_op], {Gs_beta_in: Gs_beta})
                if run_D_reg:
                    tflib.run(D_reg_op)

            # Slow path with gradient accumulation.
            else:
                for _round in rounds:
                    tflib.run(G_train_op)
                    if run_G_reg:
                        tflib.run(G_reg_op)
                tflib.run(Gs_update_op, {Gs_beta_in: Gs_beta})
                for _round in rounds:
                    tflib.run(data_fetch_op)
                    tflib.run(D_train_op)
                    if run_D_reg:
                        tflib.run(D_reg_op)

            # Run validation.
            if aug is not None:
                aug.run_validation(minibatch_size=minibatch_size)

        # Tune augmentation parameters.
        if aug is not None:
            aug.tune(minibatch_size * minibatch_repeats)

        # Perform maintenance tasks once per tick.
        done = (cur_nimg >= total_kimg * 1000) or (abort_fn is not None and abort_fn())
        if done or cur_tick < 0 or cur_nimg >= tick_start_nimg + kimg_per_tick * 1000:
            cur_tick += 1
            tick_kimg = (cur_nimg - tick_start_nimg) / 1000.0
            tick_start_nimg = cur_nimg
            tick_end_time = time.time()
            total_time = tick_end_time - start_time
            tick_time = tick_end_time - tick_start_time

            # Report progress.
            print(' '.join([
                f"tick {autosummary('Progress/tick', cur_tick):<5d}",
                f"kimg {autosummary('Progress/kimg', cur_nimg / 1000.0):<8.1f}",
                f"time {dnnlib.util.format_time(autosummary('Timing/total_sec', total_time)):<12s}",
                f"sec/tick {autosummary('Timing/sec_per_tick', tick_time):<7.1f}",
                f"sec/kimg {autosummary('Timing/sec_per_kimg', tick_time / tick_kimg):<7.2f}",
                f"maintenance {autosummary('Timing/maintenance_sec', maintenance_time):<6.1f}",
                f"gpumem {autosummary('Resources/peak_gpu_mem_gb', peak_gpu_mem_op.eval() / 2 ** 30):<5.1f}",
                f"augment {autosummary('Progress/augment', aug.strength if aug is not None else 0):.3f}",
            ]))
            autosummary('Timing/total_hours', total_time / (60.0 * 60.0))
            autosummary('Timing/total_days', total_time / (24.0 * 60.0 * 60.0))
            if progress_fn is not None:
                progress_fn(cur_nimg // 1000, total_kimg)

            # Save snapshots.
            # Tabriz: Need to save also E, D_H, D_J
            if image_snapshot_ticks is not None and (done or cur_tick % image_snapshot_ticks == 0):
                grid_fakes = Gs.run(grid_latents, grid_labels, is_validation=True, minibatch_size=minibatch_gpu)[0]
                save_image_grid(grid_fakes, os.path.join(run_dir, f'fakes{cur_nimg // 1000:06d}.png'), drange=[-1, 1],
                                grid_size=grid_size)

            if network_snapshot_ticks is not None and (done or cur_tick % network_snapshot_ticks == 0):
                pkl = os.path.join(run_dir, f'network-snapshot-{cur_nimg // 1000:06d}.pkl')
                with open(pkl, 'wb') as f:
                    pickle.dump((G, D, Gs, E, D_H, D_J), f)
                if len(metrics):
                    print('Evaluating metrics...')
                    for metric in metrics:
                        metric.run(pkl, num_gpus=num_gpus)

            # Update summaries.
            for metric in metrics:
                metric.update_autosummaries()
            tflib.autosummary.save_summaries(summary_log, cur_nimg)
            tick_start_time = time.time()
            maintenance_time = tick_start_time - tick_end_time

    print()
    print('Exiting...')
    summary_log.close()
    training_set.close()

# ----------------------------------------------------------------------------
