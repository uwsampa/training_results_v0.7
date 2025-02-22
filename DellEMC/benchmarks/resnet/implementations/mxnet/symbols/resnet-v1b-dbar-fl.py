# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

'''
Adapted from https://github.com/tornadomeet/ResNet/blob/master/symbol_resnet.py
(Original author Wei Wu) by Antti-Pekka Hynninen

"Flexible Layout" (fl) version created by Dick Carter.

Implementing the original resnet ILSVRC 2015 winning network from:

Kaiming He, Xiangyu Zhang, Shaoqing Ren, Jian Sun. "Deep Residual Learning for Image Recognition"
'''
import mxnet as mx
import numpy as np
import random
from mlperf_log_utils import _mx_resnet_print, \
    resnet_max_pool_log, resnet_conv2d_log, resnet_batchnorm_log, \
    resnet_relu_log, resnet_dense_log, \
    resnet_begin_block_log, resnet_end_block_log, resnet_projection_log

# used by group_bn initialization
from mxnet.base import check_call, _LIB, c_array
import ctypes
from mpi4py import MPI

# this is a dummy list to collect initialization records to guarantee they
# won't be destroied by garabge collector
anti_gc=[]

# FIXME: evntually to be moved into FW
def bn_group_to_sync_depth(bn_group):
    #this is log2 array to determine the number of required sync steps per bn_group
    bn2sync=[0,0,1,1,2,2,2,2,3]
    sync_depth = bn2sync[bn_group]
    return sync_depth

def handler_bytes():
    return 64 # FIXME: add FW function returning a real size measurement


# Transform a symbol from one layout to another, or do nothing if they have the same layout
def transform_layout(data, from_layout, to_layout):
    supported_layouts = ['NCHW', 'NHWC']
    if from_layout not in supported_layouts:
        raise ValueError('Not prepared to handle layout: {}'.format(from_layout))
    if to_layout not in supported_layouts:
        raise ValueError('Not prepared to handle layout: {}'.format(to_layout))

    # Insert transpose if from_layout and to_layout don't match
    if from_layout == 'NCHW' and to_layout == 'NHWC':
        return mx.sym.transpose(data, axes=(0, 2, 3, 1))
    elif from_layout == 'NHWC' and to_layout == 'NCHW':
        return mx.sym.transpose(data, axes=(0, 3, 1, 2))
    else:
        return data

def dual_scale_bias_add_relu(data, addend, io_layout, batchnorm_layout,
        data_equiv_scale, data_equiv_bias, data_saved_mean, data_saved_inv_std, data_gamma_out, data_beta_out,
        addend_equiv_scale, addend_equiv_bias, addend_saved_mean, addend_saved_inv_std, addend_gamma_out, addend_beta_out, name,
        **kwargs):
    transposed_as_needed = transform_layout(data, io_layout, batchnorm_layout)
    bn_axis = 3 if batchnorm_layout == 'NHWC' else 1
    batchnormed, _  = mx.sym.ScaleBiasAddRelu(dataX = data, dataZ = addend,
                                x_equiv_scale = data_equiv_scale , x_equiv_bias = data_equiv_bias, 
                                z_equiv_scale = addend_equiv_scale, z_equiv_bias = addend_equiv_bias,
                                x_gamma = data_gamma_out, x_beta = data_beta_out, x_mean = data_saved_mean,
                                x_invvar = data_saved_inv_std, z_gamma = addend_gamma_out, z_beta = addend_beta_out,
                                z_mean = addend_saved_mean, z_invvar = addend_saved_inv_std, layout = 'NHWC',
                                act_type = 'relu', name = name)
    # Transpose back to i/o layout as needed
    return transform_layout(batchnormed, batchnorm_layout, io_layout)

def scale_bias_add_relu(data, addend, io_layout, batchnorm_layout,
        data_equiv_scale, data_equiv_bias, data_saved_mean, data_saved_inv_std, data_gamma_out, data_beta_out,name,
        **kwargs):
    transposed_as_needed = transform_layout(data, io_layout, batchnorm_layout)
    bn_axis = 3 if batchnorm_layout == 'NHWC' else 1
    batchnormed, _  = mx.sym.ScaleBiasAddRelu(dataX = data, dataZ = addend,
                                x_equiv_scale = data_equiv_scale , x_equiv_bias = data_equiv_bias, 
                                x_gamma = data_gamma_out, x_beta = data_beta_out, x_mean = data_saved_mean,
                                x_invvar = data_saved_inv_std,
                                dual_scale_bias = False, fused_add = True,
                                layout = 'NHWC', act_type='relu', name=name)
    # Transpose back to i/o layout as needed
    return transform_layout(batchnormed, batchnorm_layout, io_layout)

def batchnorm(rank, data, io_layout, batchnorm_layout, bn_group=1, local_gpus=8, local_comm=None, **kwargs):
    # Transpose as needed to batchnorm_layout
    transposed_as_needed = transform_layout(data, io_layout, batchnorm_layout)
    bn_axis = 3 if batchnorm_layout == 'NHWC' else 1

    xbuf_ptr = (ctypes.c_void_p * local_gpus)()

    if bn_group>1:
        sync_depth = bn_group_to_sync_depth(bn_group)
        if local_comm is not None:
            handler = np.zeros(handler_bytes(),dtype=np.byte)
            check_call(_LIB.MXInitXBufSingle(rank, sync_depth, xbuf_ptr, handler.ctypes.data_as(ctypes.c_void_p)))
            handlers = np.asarray([np.zeros(handler_bytes(),dtype=np.byte)]*local_gpus)
            local_comm.Allgather([handler, handler_bytes(), MPI.BYTE], [handlers, handler_bytes(), MPI.BYTE])
            (_LIB.MXOpenIpcHandles(rank, local_gpus, sync_depth, xbuf_ptr, handlers.ctypes.data_as(ctypes.c_void_p)))
        else:
            check_call(_LIB.MXInitXBuf(local_gpus, sync_depth, xbuf_ptr))

    anti_gc.append(xbuf_ptr)
    batchnormed = mx.sym.BatchNorm(data=transposed_as_needed, axis=bn_axis, bn_group=bn_group, xbuf_ptr=ctypes.addressof(xbuf_ptr), **kwargs)

    # Transpose back to i/o layout as needed
    return transform_layout(batchnormed, batchnorm_layout, io_layout)


# A BatchNormAddRelu wrapper that responds to the input layout
def batchnorm_add_relu(rank, data, addend, io_layout, batchnorm_layout, bn_group, local_gpus, local_comm, **kwargs):
    # Transpose as needed to batchnorm_layout
    transposed_data_as_needed = transform_layout(data, io_layout, batchnorm_layout)
    transposed_addend_as_needed = transform_layout(addend, io_layout, batchnorm_layout)
    bn_axis = 3 if batchnorm_layout == 'NHWC' else 1

    xbuf_ptr = (ctypes.c_void_p * local_gpus)()

    if bn_group>1:
        sync_depth = bn_group_to_sync_depth(bn_group)
        if local_comm is not None:
            handler = np.zeros(handler_bytes(),dtype=np.byte)
            check_call(_LIB.MXInitXBufSingle(rank, sync_depth, xbuf_ptr, handler.ctypes.data_as(ctypes.c_void_p)))
            handlers = np.asarray([np.zeros(handler_bytes(),dtype=np.byte)]*local_gpus)
            local_comm.Allgather([handler, handler_bytes(), MPI.BYTE], [handlers, handler_bytes(), MPI.BYTE])
            (_LIB.MXOpenIpcHandles(rank, local_gpus, sync_depth, xbuf_ptr, handlers.ctypes.data_as(ctypes.c_void_p)))
        else:
            check_call(_LIB.MXInitXBuf(local_gpus, sync_depth, xbuf_ptr))

    anti_gc.append(xbuf_ptr)
    batchnormed = mx.sym.BatchNormAddRelu(data=transposed_data_as_needed,
                                      addend=transposed_addend_as_needed,
                                      axis=bn_axis, bn_group=bn_group, xbuf_ptr=ctypes.addressof(xbuf_ptr), **kwargs)
    # Transpose back to i/o layout as needed
    return transform_layout(batchnormed, batchnorm_layout, io_layout)

# A Pooling wrapper that responds to the input layout
def pooling(data, io_layout, pooling_layout, **kwargs):
    # Pooling kernel, as specified by pooling_layout, may be in conflict with i/o layout.
    transposed_as_needed = transform_layout(data, io_layout, pooling_layout)
    pooled = mx.sym.Pooling(data=transposed_as_needed, layout=pooling_layout, **kwargs)
    # Transpose back to i/o layout as needed
    return transform_layout(pooled, pooling_layout, io_layout)

# Calculate the output shape for a given set of convolution parameters and input shape
# The number of elements in each feature plane of the input
def element_count(nchw_inshape):
    (n, c, h, w) = nchw_inshape
    return n*h*w

# Assumption is that data comes in and out in the 'conv_layout' format.
# If this format is different from the 'batchnorm_layout' format, then the batchnorm() routine
# will introduce transposes on both sides of the mx.sym.BatchNorm symbol
def residual_unit_norm_conv(rank, data, nchw_inshape, num_filter, stride, dim_match, name, bottle_neck=True,
                  workspace=256, memonger=False, conv_layout='NCHW', batchnorm_layout='NCHW',
                  verbose=False, cudnn_bn_off=False, bn_eps=2e-5, bn_mom=0.9, conv_algo=-1,
                  fuse_bn_relu=False, fuse_bn_add_relu=False, cudnn_tensor_core_only=False,
                  bn_group=1, local_gpus=None, local_comm=None):
    """Return ResNet Unit symbol for building ResNet
    Parameters
    ----------
    data : str
        Input data
    nchw_inshape : tuple of int
        Input minibatch shape in (n, c, h, w) format independent of actual layout
    num_filter : int
        Number of output channels
    bnf : int
        Bottle neck channels factor with regard to num_filter
    stride : tuple
        Stride used in convolution
    dim_match : Boolean
        True means channel number between input and output is the same, otherwise means differ
    name : str
        Base name of the operators
    workspace : int
        Workspace used in convolution operator

    Returns
    -------
    (sym, nchw_outshape)

    sym : the model symbol (up to this point)

    nchw_output : tuple
        ( batch_size, features, height, width)
    """

    batch_size = nchw_inshape[0]
    shape = nchw_inshape[1:]
    act = 'relu' if fuse_bn_relu else None
    if bottle_neck:
        shape = resnet_begin_block_log(shape)
        # 1st NormalizedConvolution: [no Stats Apply] [no Relu] Convolution Stats-Gen
        (conv1, conv1_sum, conv1_sum_squares) = \
            mx.sym.NormConvolution(data, no_norm=True, act_type=None,
                                   num_filter=int(num_filter*0.25), kernel=(1,1), stride=(1,1), pad=(0,0),
                                   name=name + '_conv1', layout=conv_layout)
        shape = resnet_conv2d_log(shape, 1, int(num_filter*0.25), False)
        shape = resnet_batchnorm_log(shape, momentum=bn_mom, eps=bn_eps, center=True, scale=True, training=True)

        # Second NormalizedConvolution: Stats-Apply Relu Convolution Stats-Gen
        (conv2, conv2_sum, conv2_sum_squares) = \
            mx.sym.NormConvolution(conv1, no_norm=False, in_sum=conv1_sum, in_sum_squares=conv1_sum_squares, act_type='relu',
                                   num_filter=int(num_filter*0.25), kernel=(3,3), stride=stride, pad=(1,1),
                                   eps=bn_eps, momentum=bn_mom, fix_gamma=False,
                                   name=name + '_conv2', layout=conv_layout)
        shape = resnet_relu_log(shape)
        shape = resnet_conv2d_log(shape, stride, int(num_filter*0.25), False)
        shape = resnet_batchnorm_log(shape, momentum=bn_mom, eps=bn_eps, center=True, scale=True, training=True)

        # Third NormalizedConvolution: Stats-Apply Relu Convolution [no Stats-Gen]
        # The 'no stats-gen' mode is triggered by just not using the stats outputs for anything.
        (conv3, conv3_sum, conv3_sum_squares) = \
            mx.sym.NormConvolution(conv2, no_norm=False, in_sum=conv2_sum, in_sum_squares=conv2_sum_squares, act_type='relu',
                                   num_filter=num_filter, kernel=(1,1), stride=(1,1), pad=(0,0),
                                   eps=bn_eps, momentum=bn_mom, fix_gamma=False,
                                   name=name + '_conv3', layout=conv_layout)
        shape = resnet_relu_log(shape)
        shape = resnet_conv2d_log(shape, 1, int(num_filter), False)
        elem_count = element_count((batch_size,) + shape)
        (bn3_equiv_scale, bn3_equiv_bias, bn3_saved_mean, bn3_saved_inv_std, bn3_gamma_out, bn3_beta_out) = \
            mx.sym.BNStatsFinalize(sum=conv3_sum, sum_squares=conv3_sum_squares,
                                   eps=bn_eps, momentum=bn_mom, fix_gamma=False,
                                   output_mean_var=True, elem_count=elem_count, name=name + '_bn3')
        shape = resnet_batchnorm_log(shape, momentum=bn_mom, eps=bn_eps, center=True, scale=True, training=True)
        dbar = True     
        if dim_match:
            shortcut = data
            dbar = False
        else:
            if stride == (1,1) and fuse_bn_add_relu:
                #NormalizedConvolution: [no Stats Apply] [no Relu] Convolution Stats-Gen
                (shortcut, conv1sc_sum, conv1sc_sum_squares) = \
                    mx.sym.NormalizedConvolution(data, no_equiv_scale_bias=True, act_type=None,
                            num_filter=int(num_filter), kernel=(1,1), stride=(1,1), pad=(0,0),
                            name=name + '_conv1sc', layout=conv_layout)
                shape = resnet_conv2d_log(shape, 1, int(num_filter), False)
                elem_count = element_count((batch_size,) + shape)
                (bn1sc_equiv_scale, bn1sc_equiv_bias, bn1sc_saved_mean, bn1sc_saved_inv_std, bn1sc_gamma_out, bn1sc_beta_out) = \
                mx.sym.BNStatsFinalize(sum=conv1sc_sum, sum_squares=conv1sc_sum_squares,
                                   eps=bn_eps, momentum=bn_mom, fix_gamma=False,
                                   output_mean_var=True, elem_count=elem_count, name=name + '_bn1sc')
                shape = resnet_batchnorm_log(shape, momentum=bn_mom, eps=bn_eps, center=True, scale=True, training=True)

            else:
                dbar = False
                conv1sc = mx.sym.Convolution(data=data, num_filter=num_filter, kernel=(1,1), stride=stride, no_bias=True,
                                            workspace=workspace, name=name+'_conv1sc', layout=conv_layout,
                                         cudnn_algo_verbose=verbose,
                                         cudnn_algo_fwd=conv_algo, cudnn_algo_bwd_data=conv_algo, cudnn_algo_bwd_filter=conv_algo,
                                         cudnn_tensor_core_only=cudnn_tensor_core_only)
                proj_shape = resnet_conv2d_log(nchw_inshape[1:], stride, int(num_filter), False)
                shortcut = batchnorm(rank=rank, data=conv1sc, io_layout=conv_layout, batchnorm_layout=batchnorm_layout,
                                 fix_gamma=False, eps=bn_eps, momentum=bn_mom, name=name + '_bn_sc', cudnn_off=cudnn_bn_off)
                proj_shape = resnet_batchnorm_log(proj_shape, momentum=bn_mom, eps=bn_eps, center=True, scale=True, training=True)
                proj_shape = resnet_projection_log(nchw_inshape[1:], proj_shape)

        if memonger:
            shortcut._set_attr(mirror_stage='True')
        if fuse_bn_add_relu:
            shape = resnet_batchnorm_log(shape, momentum=bn_mom, eps=bn_eps, center=True, scale=True, training=True)
            shape = resnet_end_block_log(shape)
            shape = resnet_relu_log(shape)
            nchw_shape = (batch_size, ) + shape
            if dbar:
                return dual_scale_bias_add_relu(data=conv3, addend=shortcut, io_layout=conv_layout, batchnorm_layout=batchnorm_layout,
                        data_equiv_scale=bn3_equiv_scale, data_equiv_bias=bn3_equiv_bias, data_saved_mean=bn3_saved_mean,
                        data_saved_inv_std=bn3_saved_inv_std, data_gamma_out=bn3_gamma_out, data_beta_out=bn3_beta_out,
                        addend_equiv_scale = bn1sc_equiv_scale, addend_equiv_bias = bn1sc_equiv_bias, addend_saved_mean = bn1sc_saved_mean,
                        addend_saved_inv_std = bn1sc_saved_inv_std, addend_gamma_out = bn1sc_gamma_out, addend_beta_out = bn1sc_beta_out,
                        fix_gamma=False, eps=bn_eps, momentum=bn_mom, name=name + '_dbar3', cudnn_off=cudnn_bn_off),nchw_shape
            else:
                return scale_bias_add_relu(data=conv3, addend=shortcut, io_layout=conv_layout, batchnorm_layout=batchnorm_layout,
                        data_equiv_scale=bn3_equiv_scale, data_equiv_bias=bn3_equiv_bias, data_saved_mean=bn3_saved_mean,
                        data_saved_inv_std=bn3_saved_inv_std, data_gamma_out=bn3_gamma_out, data_beta_out=bn3_beta_out,
                        fix_gamma=False, eps=bn_eps, momentum=bn_mom, name=name + '_sbar3', cudnn_off=cudnn_bn_off),nchw_shape
        else:
            bn3 = batchnorm(rank=rank, data=conv3, io_layout=conv_layout, batchnorm_layout=batchnorm_layout,
                            fix_gamma=False, eps=bn_eps, momentum=bn_mom, name=name + '_bn3', cudnn_off=cudnn_bn_off)
            shape = resnet_batchnorm_log(shape, momentum=bn_mom, eps=bn_eps, center=True, scale=True, training=True)
            shape = resnet_end_block_log(shape)
            shape = resnet_relu_log(shape)
            nchw_shape = (batch_size, ) + shape
            return mx.sym.Activation(data=bn3 + shortcut, act_type='relu', name=name + '_relu3'), nchw_shape

    else:
        raise NotImplementedError 

def resnet(rank, units, num_stages, filter_list, num_classes, image_shape, batch_size, bottle_neck=True, workspace=256, dtype='float32', memonger=False,
           input_layout='NCHW', conv_layout='NCHW',  batchnorm_layout='NCHW', pooling_layout='NCHW', verbose=False,
           cudnn_bn_off=False, bn_eps=2e-5, bn_mom=0.9, conv_algo=-1,
           fuse_bn_relu=False, fuse_bn_add_relu=False, force_tensor_core=False, use_dali=True, norm_conv=True, label_smoothing = 0.0,
           bn_group=1, local_gpus=None, local_comm=None):
    """Return ResNet symbol of
    Parameters
    ----------
    units : list
        Number of units in each stage
    num_stages : int
        Number of stage
    filter_list : list
        Channel size of each stage
    num_classes : int
        Ouput size of symbol
    image_shape : tuple of int
        A 3-element tuple comprising (features, height, width) of each image
    batch_size : int
        The number of images in the training mini-batch
    dataset : str
        Dataset type, only cifar10 and imagenet supports
    workspace : int
        Workspace used in convolution operator
    dtype : str
        Precision (float32 or float16)
    memonger : boolean
        Activates "memory monger" to reduce the model's memory footprint
    input_layout : str
        interpretation (e.g. NCHW vs NHWC) of data provided by the i/o pipeline (may introduce transposes
        if in conflict with 'layout' above)
    conv_layout : str
        interpretation (e.g. NCHW vs NHWC) of data for convolution operation.
    batchnorm_layout : str
        directs which kernel performs the batchnorm (may introduce transposes if in conflict with 'conv_layout' above)
    pooling_layout : str
        directs which kernel performs the pooling (may introduce transposes if in conflict with 'conv_layout' above)
    """

    act = 'relu' if fuse_bn_relu else None
    num_unit = len(units)
    assert(num_unit == num_stages)
    data = mx.sym.Variable(name='data')
    if not use_dali:
        # double buffering of data
        if dtype == 'float32':
            data = mx.sym.identity(data=data, name='id')
        else:
            if dtype == 'float16':
                data = mx.sym.Cast(data=data, dtype=np.float16)
    (nchannel, height, width) = image_shape

    # Insert transpose as needed to get the input layout to match the desired processing layout
    data = transform_layout(data, input_layout, conv_layout)
    res_unit = residual_unit_norm_conv

    shape = image_shape
    body = mx.sym.Convolution(data=data, num_filter=filter_list[0], kernel=(7, 7), stride=(2,2), pad=(3, 3),
                              no_bias=True, name="conv0", workspace=workspace, layout=conv_layout,
                              cudnn_algo_verbose=verbose,
                              cudnn_algo_fwd=conv_algo, cudnn_algo_bwd_data=conv_algo, cudnn_algo_bwd_filter=conv_algo,
                              cudnn_tensor_core_only=force_tensor_core)
    shape = resnet_conv2d_log(shape, 2, filter_list[0], False)
    body = batchnorm(rank=rank, data=body, io_layout=conv_layout, batchnorm_layout=batchnorm_layout,
                     fix_gamma=False, eps=bn_eps, momentum=bn_mom, name='bn0', cudnn_off=cudnn_bn_off, act_type=act, 
                     bn_group=bn_group, local_gpus=local_gpus, local_comm=local_comm)
    shape = resnet_batchnorm_log(shape, momentum=bn_mom, eps=bn_eps, center=True, scale=True, training=True)
    if not fuse_bn_relu:
        body = mx.sym.Activation(data=body, act_type='relu', name='relu0')
    shape = resnet_relu_log(shape)
    body = pooling(data=body, io_layout=conv_layout, pooling_layout=pooling_layout,
                   kernel=(3, 3), stride=(2, 2), pad=(1, 1), pool_type='max')
    shape = resnet_max_pool_log(shape, 2)

    nchw_shape = (batch_size, ) + shape

    for i in range(num_stages):
        body, nchw_shape = res_unit(rank, body, nchw_shape, filter_list[i+1], (1 if i==0 else 2, 1 if i==0 else 2), False,
                                    name='stage%d_unit%d' % (i + 1, 1),
                                    bottle_neck=bottle_neck, workspace=workspace,
                                    memonger=memonger, conv_layout=conv_layout, batchnorm_layout=batchnorm_layout,
                                    verbose=verbose, cudnn_bn_off=cudnn_bn_off, bn_eps=bn_eps, bn_mom=bn_mom,
                                    conv_algo=conv_algo, fuse_bn_relu=fuse_bn_relu, fuse_bn_add_relu=fuse_bn_add_relu,
                                    cudnn_tensor_core_only=force_tensor_core,
                                    bn_group=bn_group, local_gpus=local_gpus, local_comm=local_comm)
        for j in range(units[i]-1):
            body, nchw_shape = res_unit(rank, body, nchw_shape, filter_list[i+1], (1,1), True, name='stage%d_unit%d' % (i + 1, j + 2),
                                        bottle_neck=bottle_neck, workspace=workspace,
                                        memonger=memonger, conv_layout=conv_layout, batchnorm_layout=batchnorm_layout,
                                        verbose=verbose, cudnn_bn_off=cudnn_bn_off, bn_eps = bn_eps, bn_mom=bn_mom,
                                        conv_algo=conv_algo, fuse_bn_relu=fuse_bn_relu, fuse_bn_add_relu=fuse_bn_add_relu,
                                        cudnn_tensor_core_only=force_tensor_core,
                                        bn_group=bn_group, local_gpus=local_gpus, local_comm=local_comm)
    shape = nchw_shape[1:]
    # Although kernel is not used here when global_pool=True, we should put one
    pool1 = pooling(data=body, io_layout=conv_layout, pooling_layout=pooling_layout,
                    global_pool=True, kernel=(7, 7), pool_type='avg', name='pool1')
    flat = mx.sym.Flatten(data=pool1)
    shape = (shape[0])
    fc1 = mx.sym.FullyConnected(data=flat, num_hidden=num_classes, name='fc1', cublas_algo_verbose=verbose)
    shape = resnet_dense_log(shape, num_classes)

    if dtype == 'float16':
        fc1 = mx.sym.Cast(data=fc1, dtype=np.float32)

    ##########################################################################
    # MXNet computes Cross Entropy loss gradients without explicitly computing
    # the value of loss function.
    # Take a look here:
    # https://mxnet.incubator.apache.org/api/python/symbol/symbol.html#mxnet.symbol.SoftmaxOutput
    # for further details
    ##########################################################################
    return mx.sym.SoftmaxOutput(data=fc1, name='softmax', smooth_alpha=label_smoothing)

def get_symbol(num_classes, num_layers, image_shape, conv_workspace=1024, dtype='float32',
               input_layout='NCHW', conv_layout='NCHW', batchnorm_layout='NCHW', pooling_layout='NCHW',
               verbose=False, seed=None, cudnn_bn_off=False, batchnorm_eps=2e-5, batchnorm_mom=0.9,
               conv_algo=-1, fuse_bn_relu=False, fuse_bn_add_relu=False, force_tensor_core=False, use_dali=True, label_smoothing = 0.0, **kwargs):
    """
    Adapted from https://github.com/tornadomeet/ResNet/blob/master/symbol_resnet.py
    (Original author Wei Wu) by Antti-Pekka Hynninen
    Implementing the original resnet ILSVRC 2015 winning network from:
    Kaiming He, Xiangyu Zhang, Shaoqing Ren, Jian Sun. "Deep Residual Learning for Image Recognition"
    """

    image_shape = [int(l) for l in image_shape.split(',')]
    (nchannel, height, width) = image_shape
    # This version of the Resnet model has the minibatch shape, including batch_size, 'baked-in',
    # so it cannot be bound with arbitrary inputs like most other MXNet symbols.
    if 'horovod' in kwargs['kv_store']:
        per_gpu_batch_size = int(kwargs['batch_size'])
    else:
        per_gpu_batch_size = int(kwargs['batch_size'] / len(kwargs['gpus'].split(',')))
    if height <= 28:
        num_stages = 3
        if (num_layers-2) % 9 == 0 and num_layers >= 164:
            per_unit = [(num_layers-2)//9]
            filter_list = [16, 64, 128, 256]
            bottle_neck = True
        elif (num_layers-2) % 6 == 0 and num_layers < 164:
            per_unit = [(num_layers-2)//6]
            filter_list = [16, 16, 32, 64]
            bottle_neck = False
        else:
            raise ValueError("no experiments done on num_layers {}, you can do it yourself".format(num_layers))
        units = per_unit * num_stages
    else:
        if num_layers >= 50:
            filter_list = [64, 256, 512, 1024, 2048]
            bottle_neck = True
        else:
            filter_list = [64, 64, 128, 256, 512]
            bottle_neck = False
        num_stages = 4
        if num_layers == 18:
            units = [2, 2, 2, 2]
        elif num_layers == 34:
            units = [3, 4, 6, 3]
        elif num_layers == 50:
            units = [3, 4, 6, 3]
        elif num_layers == 101:
            units = [3, 4, 23, 3]
        elif num_layers == 152:
            units = [3, 8, 36, 3]
        elif num_layers == 200:
            units = [3, 24, 36, 3]
        elif num_layers == 269:
            units = [3, 30, 48, 8]
        else:
            raise ValueError("no experiments done on num_layers {}, you can do it yourself".format(num_layers))

    # group bn 
    gpu_per_process = len(kwargs.get('gpus').split(','))
    # here local refers to current node
    local_gpus = 0
    local_comm = None
    bn_group = kwargs.get('bn_group')
    if bn_group>1:
        if gpu_per_process>1:
            # assumption is that this is kvstore case
            local_gpus = gpu_per_process
            raise ValueError("While the infrastructure is there, group_bn is currently not supported for device=kvstore. Cancel this exception to try.")
        else:
            global_comm = MPI.COMM_WORLD
            local_comm = global_comm.Split_type(MPI.COMM_TYPE_SHARED)
            local_gpus=local_comm.Get_size()
       

    # FIXME: use kwargs through
    return resnet(rank              = kwargs.get('local_rank'),
                  units             = units,
                  num_stages        = num_stages,
                  filter_list       = filter_list,
                  num_classes       = num_classes,
                  image_shape       = image_shape,
                  batch_size        = per_gpu_batch_size,
                  bottle_neck       = bottle_neck,
                  workspace         = conv_workspace,
                  dtype             = dtype,
                  input_layout      = input_layout,
                  conv_layout       = conv_layout,
                  batchnorm_layout  = batchnorm_layout,
                  pooling_layout    = pooling_layout,
                  verbose           = verbose,
                  cudnn_bn_off      = cudnn_bn_off,
                  bn_eps            = batchnorm_eps,
                  bn_mom            = batchnorm_mom,
                  conv_algo         = conv_algo,
                  fuse_bn_relu      = fuse_bn_relu,
                  fuse_bn_add_relu  = fuse_bn_add_relu,
                  force_tensor_core = force_tensor_core,
                  use_dali          = use_dali,
                  label_smoothing   = label_smoothing,
                  bn_group          = bn_group,
                  local_gpus        = local_gpus,
                  local_comm        = local_comm)
