from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import os
import sys
import json
import random

import tensorflow as tf
import multiprocessing

import Utilities
from Conv2dUtilities import Conv2dUtilities
from KernelPrediction import KernelPrediction
from FeatureFlags import FeatureFlags

from UNet import UNet
from Tiramisu import Tiramisu

from DataAugmentation import DataAugmentation
from LossDifference import LossDifference
from LossDifference import LossDifferenceEnum
from RenderPasses import RenderPasses
from FeatureEngineering import FeatureEngineering

parser = argparse.ArgumentParser(description='Training and inference for the DeepDenoiser.')

parser.add_argument(
    'json_filename',
    help='The json specifying all the relevant details.')

parser.add_argument(
    '--validate', action="store_true",
    help='Perform a validation step.')

parser.add_argument(
    '--batch_size', type=int, default=4,
    help='Number of tiles to process in a batch')

parser.add_argument(
    '--threads', default=multiprocessing.cpu_count() + 1,
    help='Number of threads to use')

parser.add_argument(
    '--train_epochs', type=int, default=10000,
    help='Number of epochs to train.')

parser.add_argument(
    '--validation_interval', type=int, default=1,
    help='Number of epochs after which a validation is made.')

parser.add_argument(
    '--data_format', type=str, default=None,
    choices=['channels_first', 'channels_last'],
    help='A flag to override the data format used in the model. channels_first '
         'provides a performance boost on GPU but is not always compatible '
         'with CPU. If left unspecified, the data format will be chosen '
         'automatically based on whether TensorFlow was built for CPU or GPU.')

def global_activation_function(features, name=None):
  # HACK: Quick way to experiment with other activation function.
  return tf.nn.relu(features, name=name)
  # return tf.nn.leaky_relu(features, name=name)
  # return tf.nn.crelu(features, name=name)
  # return tf.nn.elu(features, name=name)
  # return tf.nn.selu(features, name=name)

class FeatureStandardization:

  def __init__(self, use_log1p, mean, variance, name):
    self.use_log1p = use_log1p
    self.mean = mean
    self.variance = variance
    self.name = name

  def use_mean(self):
    return self.mean != 0.
  
  def use_variance(self):
    return self.variance != 1.

  def standardize(self, feature, index):
    if self.use_log1p:
      feature = Utilities.signed_log1p(feature)
    if self.use_mean():
      feature = tf.subtract(feature, self.mean)
    if self.use_variance():
      feature = tf.divide(feature, tf.sqrt(self.variance))
    return feature
    
  def invert_standardize(self, feature):
    if self.use_variance():
      feature = tf.multiply(feature, tf.sqrt(self.variance))
    if self.use_mean():
      feature = tf.add(feature, self.mean)
    if self.use_log1p:
      feature = Utilities.signed_expm1(feature)
    return feature


class FeatureVariance:

  def __init__(self, use_variance, relative_variance, compute_before_standardization, compress_to_one_channel, name):
    self.use_variance = use_variance
    self.relative_variance = relative_variance
    self.compute_before_standardization = compute_before_standardization
    self.compress_to_one_channel = compress_to_one_channel
    self.name = name
  
  def variance(self, inputs, epsilon=1e-4, data_format='channels_last'):
    assert self.use_variance
    result = FeatureEngineering.variance(
        inputs, relative_variance=self.relative_variance, compress_to_one_channel=self.compress_to_one_channel,
        epsilon=epsilon, data_format=data_format)
    return result
  

class PredictionFeature:

  def __init__(
      self, number_of_sources, preserve_source, is_target, feature_standardization, feature_variance,
      feature_flags, number_of_channels, name):
    
    self.number_of_sources = number_of_sources
    self.preserve_source = preserve_source
    self.is_target = is_target
    self.feature_standardization = feature_standardization
    self.feature_variance = feature_variance
    self.feature_flags = feature_flags
    self.number_of_channels = number_of_channels
    self.name = name

  def initialize_sources_from_dictionary(self, dictionary):
    self.source = []
    self.variance = []
    for index in range(self.number_of_sources):
      source_at_index = dictionary[RenderPasses.source_feature_name_indexed(self.name, index)]
      self.source.append(source_at_index)

  def standardize(self):
    if self.feature_variance.use_variance and self.feature_variance.compute_before_standardization:
      for index in range(self.number_of_sources):
        assert len(self.variance) == index
        variance = self.feature_variance.variance(self.source[index], data_format='channels_last')
        self.variance.append(variance)
    
    if self.preserve_source:
      self.preserved_source = self.source[0]
    if self.feature_standardization != None:
      for index in range(self.number_of_sources):
        self.source[index] = self.feature_standardization.standardize(self.source[index], index)
    
    if self.feature_variance.use_variance and not self.feature_variance.compute_before_standardization:
      for index in range(self.number_of_sources):
        assert len(self.variance) == index
        variance = self.feature_variance.variance(self.source[index], data_format='channels_last')
        self.variance.append(variance)
  
  def prediction_invert_standardize(self):
    if self.feature_standardization != None:
      self.prediction = self.feature_standardization.invert_standardize(self.prediction)
  
  def add_prediction(self, prediction):
    if not self.is_target:
      raise Exception('Adding a prediction for a feature that is not a target is not allowed.')
    self.prediction = prediction
  
  def add_prediction_to_dictionary(self, dictionary):
    if self.is_target:
      dictionary[RenderPasses.prediction_feature_name(self.name)] = self.prediction


class BaseTrainingFeature:

  def __init__(
      self, name, loss_difference,
      mean_weight, variation_weight, ms_ssim_weight,
      masked_mean_weight, masked_variation_weight, masked_ms_ssim_weight,
      track_mean, track_variation, track_ms_ssim,
      track_difference_histogram, track_variation_difference_histogram,
      track_masked_mean, track_masked_variation, track_masked_ms_ssim,
      track_masked_difference_histogram, track_masked_variation_difference_histogram):
      
    self.name = name
    self.loss_difference = loss_difference
    
    self.mean_weight = mean_weight
    self.variation_weight = variation_weight
    self.ms_ssim_weight = ms_ssim_weight
    self.masked_mean_weight = masked_mean_weight
    self.masked_variation_weight = masked_variation_weight
    self.masked_ms_ssim_weight = masked_ms_ssim_weight
    
    self.track_mean = track_mean
    self.track_variation = track_variation
    self.track_ms_ssim = track_ms_ssim
    self.track_difference_histogram = track_difference_histogram
    self.track_variation_difference_histogram = track_variation_difference_histogram
    self.track_masked_mean = track_masked_mean
    self.track_masked_variation = track_masked_variation
    self.track_masked_ms_ssim = track_masked_ms_ssim
    self.track_masked_difference_histogram = track_masked_difference_histogram
    self.track_masked_variation_difference_histogram = track_masked_variation_difference_histogram
  
  
  def difference(self):
    result = LossDifference.difference(self.predicted, self.target, self.loss_difference)
    return result
  
  def horizontal_variation_difference(self):
    predicted_horizontal_variation = BaseTrainingFeature.__horizontal_variation(self.predicted)
    target_horizontal_variation = BaseTrainingFeature.__horizontal_variation(self.target)
    result = LossDifference.difference(
        predicted_horizontal_variation, target_horizontal_variation, self.loss_difference)
    return result
  
  def vertical_variation_difference(self):
    predicted_vertical_variation = BaseTrainingFeature.__vertical_variation(self.predicted)
    target_vertical_variation = BaseTrainingFeature.__vertical_variation(self.target)
    result = LossDifference.difference(
        predicted_vertical_variation, target_vertical_variation, self.loss_difference)
    return result
  
  def variation_difference(self):
    result = tf.concat(
        [tf.layers.flatten(self.horizontal_variation_difference()),
        tf.layers.flatten(self.vertical_variation_difference())], axis=1)
    return result
  
  def masked_difference(self):
    result = tf.multiply(self.difference(), self.mask)
    return result
  
  def masked_variation_difference(self):
    result = tf.multiply(self.variation_difference(), self.mask)
    return result
  
  def mean(self):
    result = tf.reduce_mean(self.difference())
    return result
  
  def variation(self):
    result = tf.reduce_mean(self.variation_difference())
    return result
  
  def ms_ssim(self):
    predicted = self.predicted
    target = self.target
    
    if len(predicted.shape) == 3:
      shape = tf.shape(predicted)
      predicted = tf.reshape(predicted, [-1, shape[0], shape[1], shape[2]])
      target = tf.reshape(target, [-1, shape[0], shape[1], shape[2]])
    
    # Move channels to last position if needed.
    if predicted.shape[3] != 3:
      predicted = tf.transpose(predicted, [0, 3, 1, 2])
      target = tf.transpose(target, [0, 3, 1, 2])
    
    # Our tile size is not large enough for all power factors (0.0448, 0.2856, 0.3001, 0.2363, 0.1333)
    # Starting with the second power factor, the size is scaled down by 2 after each one. The size after
    # the downscaling has to be larger than 11 which is the filter size that is used by SSIM.
    # 64 / 2 / 2 = 16 > 11
    
    # TODO: Calculate the number of factors (DeepBlender)
    # HACK: This is far away from the actual 1e10, but we are looking for better visual results. (DeepBlender)
    # maximum_value = 1e10
    maximum_value = 1.
    ms_ssim = tf.image.ssim_multiscale(predicted, target, maximum_value, power_factors=(0.0448, 0.2856, 0.3001))
    
    result = tf.subtract(1., tf.reduce_mean(ms_ssim))
    return result
  
  def masked_mean(self):
    result = tf.cond(
        tf.greater(self.mask_sum, 0.),
        lambda: tf.reduce_sum(tf.divide(tf.multiply(self.difference(), self.mask), self.mask_sum)),
        lambda: tf.constant(0.))
    return result
  
  def masked_variation(self):
    result = tf.cond(
        tf.greater(self.mask_sum, 0.),
        lambda: tf.reduce_sum(tf.divide(tf.multiply(self.variation_difference(), self.mask), self.mask_sum)),
        lambda: tf.constant(0.))
    return result
  
  def masked_ms_ssim(self):
    raise Exception('Not implemented')
  
  
  def loss(self):
    result = 0.0
    
    if self.mean_weight > 0.0:
      result = tf.add(result, tf.scalar_mul(self.mean_weight, self.mean()))
    if self.variation_weight > 0.0:
      result = tf.add(result, tf.scalar_mul(self.variation_weight, self.variation()))
    if self.ms_ssim_weight > 0.0:
      result = tf.add(result, tf.scalar_mul(self.ms_ssim_weight, self.ms_ssim()))
    
    if self.masked_mean_weight > 0.0:
      result = tf.add(result, tf.scalar_mul(self.masked_mean_weight, self.masked_mean()))
    if self.masked_variation_weight > 0.0:
      result = tf.add(result, tf.scalar_mul(self.masked_variation_weight, self.masked_variation()))
    if self.masked_ms_ssim_weight > 0.0:
      result = tf.add(result, tf.scalar_mul(self.masked_ms_ssim_weight, self.masked_ms_ssim()))
    return result
    
  
  def add_tracked_summaries(self):
    if self.track_mean:
      tf.summary.scalar(RenderPasses.mean_name(self.name), self.mean())
    if self.track_variation:
      tf.summary.scalar(RenderPasses.variation_name(self.name), self.variation())
    if self.track_ms_ssim:
      tf.summary.scalar(RenderPasses.ms_ssim_name(self.name), self.ms_ssim())
    
    if self.track_masked_mean:
      tf.summary.scalar(RenderPasses.mean_name(self.name, True), self.masked_mean())
    if self.track_masked_variation:
      tf.summary.scalar(RenderPasses.variation_name(self.name, True), self.masked_variation())
    if self.track_masked_ms_ssim:
      tf.summary.scalar(RenderPasses.ms_ssim_name(self.name, True), self.masked_ms_ssim())
  
  def add_tracked_histograms(self):
    if self.track_difference_histogram:
      tf.summary.histogram(RenderPasses.tensorboard_name(self.name), self.difference())
    if self.track_variation_difference_histogram:
      tf.summary.histogram(RenderPasses.variation_name(self.name), self.variation_difference())
    
    if self.track_masked_difference_histogram:
      tf.summary.histogram(RenderPasses.tensorboard_name(self.name, True), self.masked_difference())
    if self.track_masked_variation_difference_histogram:
      tf.summary.histogram(RenderPasses.variation_name(self.name, True), self.masked_variation_difference())
    
  def add_tracked_metrics_to_dictionary(self, dictionary):
    if self.track_mean:
      dictionary[RenderPasses.mean_name(self.name)] = tf.metrics.mean(self.mean())
    if self.track_variation:
      dictionary[RenderPasses.variation_name(self.name)] = tf.metrics.mean(self.variation())
    if self.track_ms_ssim:
      dictionary[RenderPasses.ms_ssim_name(self.name)] = tf.metrics.mean(self.ms_ssim())
    
    if self.track_masked_mean:
      dictionary[RenderPasses.mean_name(self.name, True)] = tf.metrics.mean(self.masked_mean())
    if self.track_masked_variation:
      dictionary[RenderPasses.variation_name(self.name, True)] = tf.metrics.mean(self.masked_variation())
    if self.track_masked_ms_ssim:
      dictionary[RenderPasses.ms_ssim_name(self.name, True)] = tf.metrics.mean(self.masked_ms_ssim())

  @staticmethod
  def __horizontal_variation(image_batch):
    # 'channels_last' or NHWC
    image_batch = tf.subtract(
        BaseTrainingFeature.__shift_left(image_batch), BaseTrainingFeature.__shift_right(image_batch))
    return image_batch
    
  def __vertical_variation(image_batch):
    # 'channels_last' or NHWC
    image_batch = tf.subtract(
        BaseTrainingFeature.__shift_up(image_batch), BaseTrainingFeature.__shift_down(image_batch))
    return image_batch
    
  @staticmethod
  def __shift_left(image_batch):
    # 'channels_last' or NHWC
    axis = 2
    width = tf.shape(image_batch)[axis]
    image_batch = tf.slice(image_batch, [0, 0, 1, 0], [-1, -1, width - 1, -1])
    return(image_batch)
  
  @staticmethod
  def __shift_right(image_batch):
    # 'channels_last' or NHWC
    axis = 2
    width = tf.shape(image_batch)[axis]
    image_batch = tf.slice(image_batch, [0, 0, 0, 0], [-1, -1, width - 1, -1]) 
    return(image_batch)
  
  @staticmethod
  def __shift_up(image_batch):
    # 'channels_last' or NHWC
    axis = 1
    height = tf.shape(image_batch)[axis]
    image_batch = tf.slice(image_batch, [0, 1, 0, 0], [-1, height - 1, -1, -1]) 
    return(image_batch)

  @staticmethod
  def __shift_down(image_batch):
    # 'channels_last' or NHWC
    axis = 1
    height = tf.shape(image_batch)[axis]
    image_batch = tf.slice(image_batch, [0, 0, 0, 0], [-1, height - 1, -1, -1]) 
    return(image_batch)


class TrainingFeature(BaseTrainingFeature):

  def __init__(
      self, name, loss_difference,
      mean_weight, variation_weight, ms_ssim_weight,
      masked_mean_weight, masked_variation_weight, masked_ms_ssim_weight,
      track_mean, track_variation, track_ms_ssim,
      track_difference_histogram, track_variation_difference_histogram,
      track_masked_mean, track_masked_variation, track_masked_ms_ssim,
      track_masked_difference_histogram, track_masked_variation_difference_histogram):
    
    BaseTrainingFeature.__init__(
        self, name, loss_difference,
        mean_weight, variation_weight, ms_ssim_weight,
        masked_mean_weight, masked_variation_weight, masked_ms_ssim_weight,
        track_mean, track_variation, track_ms_ssim,
        track_difference_histogram, track_variation_difference_histogram,
        track_masked_mean, track_masked_variation, track_masked_ms_ssim,
        track_masked_difference_histogram, track_masked_variation_difference_histogram)
  
  def initialize(self, source_features, predicted_features, target_features):
    self.predicted = predicted_features[RenderPasses.prediction_feature_name(self.name)]
    self.target = target_features[RenderPasses.target_feature_name(self.name)]
    if RenderPasses.is_direct_or_indirect_render_pass(self.name):
      corresponding_color_pass = RenderPasses.direct_or_indirect_to_color_render_pass(self.name)
      corresponding_target_feature = target_features[RenderPasses.target_feature_name(corresponding_color_pass)]
      self.mask = Conv2dUtilities.non_zero_mask(corresponding_target_feature, data_format='channels_last')
      self.mask_sum = tf.reduce_sum(self.mask)


class CombinedTrainingFeature(BaseTrainingFeature):

  def __init__(
      self, name, loss_difference,
      color_training_feature, direct_training_feature, indirect_training_feature,
      mean_weight, variation_weight, ms_ssim_weight,
      masked_mean_weight, masked_variation_weight, masked_ms_ssim_weight,
      track_mean, track_variation, track_ms_ssim,
      track_difference_histogram, track_variation_difference_histogram,
      track_masked_mean, track_masked_variation, track_masked_ms_ssim,
      track_masked_difference_histogram, track_masked_variation_difference_histogram):
    
    BaseTrainingFeature.__init__(
        self, name, loss_difference,
        mean_weight, variation_weight, ms_ssim_weight,
        masked_mean_weight, masked_variation_weight, masked_ms_ssim_weight,
        track_mean, track_variation, track_ms_ssim,
        track_difference_histogram, track_variation_difference_histogram,
        track_masked_mean, track_masked_variation, track_masked_ms_ssim,
        track_masked_difference_histogram, track_masked_variation_difference_histogram)
    
    self.color_training_feature = color_training_feature
    self.direct_training_feature = direct_training_feature
    self.indirect_training_feature = indirect_training_feature
  
  def initialize(self, source_features, predicted_features, target_features):
    self.predicted = tf.multiply(
        self.color_training_feature.predicted,
        tf.add(
            self.direct_training_feature.predicted,
            self.indirect_training_feature.predicted))

    self.target = tf.multiply(
        self.color_training_feature.target,
        tf.add(
            self.direct_training_feature.target,
            self.indirect_training_feature.target))
  
  
class CombinedImageTrainingFeature(BaseTrainingFeature):

  def __init__(
      self, name, loss_difference,
      diffuse_training_feature, glossy_training_feature,
      subsurface_training_feature, transmission_training_feature,
      emission_training_feature, environment_training_feature,
      mean_weight, variation_weight, ms_ssim_weight,
      masked_mean_weight, masked_variation_weight, masked_ms_ssim_weight,
      track_mean, track_variation, track_ms_ssim,
      track_difference_histogram, track_variation_difference_histogram,
      track_masked_mean, track_masked_variation, track_masked_ms_ssim,
      track_masked_difference_histogram, track_masked_variation_difference_histogram):
    
    BaseTrainingFeature.__init__(
        self, name, loss_difference,
        mean_weight, variation_weight, ms_ssim_weight,
        masked_mean_weight, masked_variation_weight, masked_ms_ssim_weight,
        track_mean, track_variation, track_ms_ssim,
        track_difference_histogram, track_variation_difference_histogram,
        track_masked_mean, track_masked_variation, track_masked_ms_ssim,
        track_masked_difference_histogram, track_masked_variation_difference_histogram)
    
    self.diffuse_training_feature = diffuse_training_feature
    self.glossy_training_feature = glossy_training_feature
    self.subsurface_training_feature = subsurface_training_feature
    self.transmission_training_feature = transmission_training_feature
    self.emission_training_feature = emission_training_feature
    self.environment_training_feature = environment_training_feature
  
  def initialize(self, source_features, predicted_features, target_features):
    self.predicted = tf.add_n([
        self.diffuse_training_feature.predicted,
        self.glossy_training_feature.predicted,
        self.subsurface_training_feature.predicted,
        self.transmission_training_feature.predicted,
        self.emission_training_feature.predicted,
        self.environment_training_feature.predicted])

    self.target = tf.add_n([
        self.diffuse_training_feature.target,
        self.glossy_training_feature.target,
        self.subsurface_training_feature.target,
        self.transmission_training_feature.target,
        self.emission_training_feature.target,
        self.environment_training_feature.target])

class TrainingFeatureLoader:

  def __init__(self, is_target, number_of_channels, name):
    self.is_target = is_target
    self.number_of_channels = number_of_channels
    self.name = name
  
  def add_to_parse_dictionary(self, dictionary, required_indices):
    for index in required_indices:
      dictionary[RenderPasses.source_feature_name_indexed(self.name, index)] = tf.FixedLenFeature([], tf.string)
    if self.is_target:
      dictionary[RenderPasses.target_feature_name(self.name)] = tf.FixedLenFeature([], tf.string)
  
  def deserialize(self, parsed_features, required_indices, height, width):
    self.source = {}
    for index in required_indices:
      self.source[index] = tf.decode_raw(
          parsed_features[RenderPasses.source_feature_name_indexed(self.name, index)], tf.float32)
      self.source[index] = tf.reshape(self.source[index], [height, width, self.number_of_channels])
    if self.is_target:
      self.target = tf.decode_raw(parsed_features[RenderPasses.target_feature_name(self.name)], tf.float32)
      self.target = tf.reshape(self.target, [height, width, self.number_of_channels])
  
  def add_to_sources_dictionary(self, sources, index_tuple):
    for i in range(len(index_tuple)):
      index = index_tuple[i]
      sources[RenderPasses.source_feature_name_indexed(self.name, i)] = self.source[index]
    
  def add_to_targets_dictionary(self, targets):
    if self.is_target:
      targets[RenderPasses.target_feature_name(self.name)] = self.target

class TrainingFeatureAugmentation:

  def __init__(self, number_of_sources, is_target, number_of_channels, name):
    self.number_of_sources = number_of_sources
    self.is_target = is_target
    self.number_of_channels = number_of_channels
    self.name = name
  
  def intialize_from_dictionaries(self, sources, targets):
    self.source = {}
    for index in range(self.number_of_sources):
      self.source[index] = (sources[RenderPasses.source_feature_name_indexed(self.name, index)])
    if self.is_target:
      self.target = targets[RenderPasses.target_feature_name(self.name)]
  
  def flip_left_right(self, data_format):
    if data_format != 'channels_last':
      raise Exception('Channel last is the only supported format.')
    for index in range(self.number_of_sources):
      self.source[index] = DataAugmentation.flip_left_right(self.source[index], self.name)
    if self.is_target:
      self.target = DataAugmentation.flip_left_right(self.target, self.name)
  
  def rotate90(self, k, data_format):
    for index in range(self.number_of_sources):
      self.source[index] = DataAugmentation.rotate90(self.source[index], k, self.name)
    if self.is_target:
      self.target = DataAugmentation.rotate90(self.target, k, self.name)
  
  def permute_rgb(self, rgb_permutation, data_format):
    if RenderPasses.is_rgb_color_render_pass(self.name):
      for index in range(self.number_of_sources):
        self.source[index] = DataAugmentation.permute_rgb(self.source[index], rgb_permutation, self.name)
      if self.is_target:
        self.target = DataAugmentation.permute_rgb(self.target, rgb_permutation, self.name)
  
  def add_to_sources_dictionary(self, sources):
    for index in range(self.number_of_sources):
      sources[RenderPasses.source_feature_name_indexed(self.name, index)] = self.source[index]
    
  def add_to_targets_dictionary(self, targets):
    if self.is_target:
      targets[RenderPasses.target_feature_name(self.name)] = self.target


def neural_network_model(inputs, output_size, is_training, data_format, use_batch_normalization=False, dropout_rate=0.0):
  unet = UNet(
      number_of_filters_for_convolution_blocks=[128, 128, 128],
      number_of_convolutions_per_block=4, number_of_output_filters=output_size,
      activation_function=global_activation_function,
      use_batch_normalization=use_batch_normalization, dropout_rate=dropout_rate,
      data_format=data_format)
  inputs = unet.unet(inputs, is_training)
  
  # tiramisu = Tiramisu(
      # number_of_preprocessing_convolution_filters=32,
      # number_of_filters_for_convolution_blocks=[16, 32, 64],
      # number_of_convolutions_per_block=4, number_of_output_filters=output_size,
      # activation_function=global_activation_function,
      # use_batch_normalization=use_batch_normalization, dropout_rate=dropout_rate,
      # data_format=data_format)
  # inputs = tiramisu.tiramisu(inputs, is_training)
  
  return inputs


def combined_features_model(prediction_features, output_prediction_features, is_training, use_kernel_predicion, kernel_size, use_single_feature_prediction, feature_flags, use_CPU_only, data_format):
  
  with tf.name_scope('prepare_network_input'):
    concat_axis = 3
    prediction_inputs = []
    auxiliary_prediction_inputs = []
    auxiliary_inputs = []
    for prediction_feature in prediction_features:
      if prediction_feature.is_target:
        for index in range(prediction_feature.number_of_sources):
          source = prediction_feature.source[index]
          if index == 0:
            prediction_inputs.append(source)
            if prediction_feature.feature_variance.use_variance:
              source_variance = prediction_feature.variance[index]
              prediction_inputs.append(source_variance)
          else:
            auxiliary_prediction_inputs.append(source)
            if prediction_feature.feature_variance.use_variance:
              source_variance = prediction_feature.variance[index]
              auxiliary_prediction_inputs.append(source_variance)
      else:
        for index in range(prediction_feature.number_of_sources):
          source = prediction_feature.source[index]
          auxiliary_inputs.append(source)
          if prediction_feature.feature_variance.use_variance:
            source_variance = prediction_feature.variance[index]
            auxiliary_inputs.append(source_variance)
          
    prediction_inputs = tf.concat(prediction_inputs, concat_axis)
    
    if len(auxiliary_prediction_inputs) > 0:
      auxiliary_prediction_inputs = tf.concat(auxiliary_prediction_inputs, concat_axis)
    else:
      auxiliary_prediction_inputs = None
    if len(auxiliary_inputs) > 0:
      auxiliary_inputs = tf.concat(auxiliary_inputs, concat_axis)
    else:
      auxiliary_inputs = None
  
  
  if data_format is None:
    # When running on GPU, transpose the data from channels_last (NHWC) to
    # channels_first (NCHW) to improve performance.
    # See https://www.tensorflow.org/performance/performance_guide#data_formats
    data_format = (
      'channels_first' if tf.test.is_built_with_cuda() else
        'channels_last')
    if use_CPU_only:
      data_format = 'channels_last'

  concat_axis = 3
  if data_format == 'channels_first':
    prediction_inputs = tf.transpose(prediction_inputs, [0, 3, 1, 2])
    if auxiliary_prediction_inputs != None:
      auxiliary_prediction_inputs = tf.transpose(auxiliary_prediction_inputs, [0, 3, 1, 2])
    if auxiliary_inputs != None:
      auxiliary_inputs = tf.transpose(auxiliary_inputs, [0, 3, 1, 2])
    concat_axis = 1
  
  output_size = 0
  for prediction_feature in output_prediction_features:
    if use_kernel_predicion:
      output_size = output_size + (kernel_size ** 2)
    else:
      output_size = output_size + prediction_feature.number_of_channels
  
  with tf.name_scope('network_input_concat'):
    concat_axis = Conv2dUtilities.channel_axis(prediction_inputs, data_format)
    
    if auxiliary_prediction_inputs == None and auxiliary_inputs == None:
      outputs = prediction_inputs
    elif auxiliary_prediction_inputs == None:
      outputs = tf.concat([prediction_inputs, auxiliary_inputs], concat_axis)
    elif auxiliary_inputs == None:
      outputs = tf.concat([prediction_inputs, auxiliary_prediction_inputs], concat_axis)
    else:
      outputs = tf.concat([prediction_inputs, auxiliary_prediction_inputs, auxiliary_inputs], concat_axis)
  
  # HACK: Implementation trick to keep tensorboard cleaner.
  #with tf.name_scope('model'):
  with tf.variable_scope('model'):
    outputs = neural_network_model(outputs, output_size, is_training, data_format)
  
  
  if data_format == 'channels_first':
    outputs = tf.transpose(outputs, [0, 2, 3, 1])
  
  concat_axis = 3
  size_splits = []
  for prediction_feature in output_prediction_features:
    if use_kernel_predicion:
      size_splits.append(kernel_size ** 2)
    else:
      size_splits.append(prediction_feature.number_of_channels)
  
  with tf.name_scope('split'):
    prediction_tuple = tf.split(outputs, size_splits, concat_axis)
  for index, prediction in enumerate(prediction_tuple):
    output_prediction_features[index].add_prediction(prediction)
  
  with tf.name_scope('invert_standardize'):
    invert_standardize = not use_kernel_predicion
    if invert_standardize:
      for prediction_feature in output_prediction_features:
        prediction_feature.prediction_invert_standardize()
  
  with tf.name_scope('use_kernel_predicion'):
    if use_kernel_predicion:
      for prediction_feature in output_prediction_features:
        assert prediction_feature.preserve_source
        prediction = KernelPrediction.kernel_prediction(
            prediction_feature.preserved_source, prediction_feature.prediction,
            kernel_size, data_format='channels_last')
        prediction_feature.add_prediction(prediction)
  
def single_feature_model(prediction_features, output_prediction_features, is_training, use_kernel_predicion, kernel_size, use_single_feature_prediction, feature_flags, use_CPU_only, data_format):
  
  with tf.name_scope('prepare_auxiliary_inputs'):
    concat_axis = 3
    auxiliary_inputs = []
    for prediction_feature in prediction_features:
      if not prediction_feature.is_target:
        for index in range(prediction_feature.number_of_sources):
          source = prediction_feature.source[index]
          auxiliary_inputs.append(source)
          if prediction_feature.feature_variance.use_variance:
            source_variance = prediction_feature.variance[index]
            auxiliary_inputs.append(source_variance)
    if len(auxiliary_inputs) > 0:
      auxiliary_inputs = tf.concat(auxiliary_inputs, concat_axis)
    else:
      auxiliary_inputs = None
  
  with tf.name_scope('single_feature_prediction'):
    reuse = False
    for prediction_feature in prediction_features:
      concat_axis = 3
      prediction_inputs = []
      auxiliary_prediction_inputs = []
      if prediction_feature.is_target:
        with tf.name_scope('prepare_network_input'):
          for index in range(prediction_feature.number_of_sources):
            source = prediction_feature.source[index]
            if index == 0:
              prediction_inputs.append(source)
              if prediction_feature.feature_variance.use_variance:
                source_variance = prediction_feature.variance[index]
                prediction_inputs.append(source_variance)
            else:
              auxiliary_prediction_inputs.append(source)
              if prediction_feature.feature_variance.use_variance:
                source_variance = prediction_feature.variance[index]
                auxiliary_prediction_inputs.append(source_variance)
        
          prediction_inputs = tf.concat(prediction_inputs, concat_axis)
          if len(auxiliary_prediction_inputs) > 0:
            auxiliary_prediction_inputs = tf.concat(auxiliary_prediction_inputs, concat_axis)
          else:
            auxiliary_prediction_inputs = None
          
          height, width = Conv2dUtilities.height_width(prediction_inputs, data_format)
          prediction_feature_flags = feature_flags.feature_flags(prediction_feature.name, height, width, data_format)
          
          if auxiliary_prediction_inputs == None and auxiliary_inputs == None:
            outputs = prediction_inputs
          elif auxiliary_prediction_inputs == None:
            outputs = tf.concat([prediction_inputs, auxiliary_inputs], concat_axis)
          elif auxiliary_inputs == None:
            outputs = tf.concat([prediction_inputs, auxiliary_prediction_inputs], concat_axis)
          else:
            outputs = tf.concat([prediction_inputs, auxiliary_prediction_inputs, auxiliary_inputs], concat_axis)

        
        if use_kernel_predicion:
          output_size = kernel_size ** 2
        else:
          output_size = prediction_feature.number_of_channels
        
        tmp_data_format = data_format
        if tmp_data_format is None:
          # When running on GPU, transpose the data from channels_last (NHWC) to
          # channels_first (NCHW) to improve performance.
          # See https://www.tensorflow.org/performance/performance_guide#data_formats
          tmp_data_format = (
            'channels_first' if tf.test.is_built_with_cuda() else
              'channels_last')
          if use_CPU_only:
            tmp_data_format = 'channels_last'
        
        
        if tmp_data_format == 'channels_first':
          outputs = tf.transpose(outputs, [0, 3, 1, 2])
          concat_axis = 1
        
        
        with tf.name_scope('model'):
          with tf.variable_scope('reused_model', reuse=reuse):
            outputs = neural_network_model(outputs, output_size, is_training, tmp_data_format)
        
        # Reuse the variables after the first pass.
        reuse = True
        
        if tmp_data_format == 'channels_first':
          outputs = tf.transpose(outputs, [0, 2, 3, 1])
        
        prediction_feature.add_prediction(outputs)
        
        with tf.name_scope('invert_standardize'):
          invert_standardize = not use_kernel_predicion
          if invert_standardize:
            prediction_feature.prediction_invert_standardize()
        
        with tf.name_scope('kernel_prediction'):
          if use_kernel_predicion:
            assert prediction_feature.preserve_source
            prediction = KernelPrediction.kernel_prediction(
                prediction_feature.preserved_source, prediction_feature.prediction,
                kernel_size, data_format='channels_last')
            prediction_feature.add_prediction(prediction)


def model(prediction_features, mode, use_kernel_predicion, kernel_size, use_single_feature_prediction, feature_flags, use_CPU_only, data_format):
  
  is_training = False
  if mode == tf.estimator.ModeKeys.TRAIN:
    is_training = True
  
  output_prediction_features = []
  for prediction_feature in prediction_features:
    if prediction_feature.is_target:
      output_prediction_features.append(prediction_feature)
        
  # Standardization of the data
  with tf.name_scope('standardize'):
    for prediction_feature in prediction_features:
      prediction_feature.standardize()

  if use_single_feature_prediction:
    single_feature_model(prediction_features, output_prediction_features, is_training, use_kernel_predicion, kernel_size, use_single_feature_prediction, feature_flags, use_CPU_only, data_format)
  else:
    combined_features_model(prediction_features, output_prediction_features, is_training, use_kernel_predicion, kernel_size, use_single_feature_prediction, feature_flags, use_CPU_only, data_format)
  
  prediction_dictionary = {}
  for prediction_feature in output_prediction_features:
    prediction_feature.add_prediction_to_dictionary(prediction_dictionary)  
  return prediction_dictionary


def model_fn(features, labels, mode, params):
  prediction_features = params['prediction_features']
  
  for prediction_feature in prediction_features:
    prediction_feature.initialize_sources_from_dictionary(features)
  
  data_format = params['data_format']
  predictions = model(
      prediction_features, mode,
      params['use_kernel_predicion'], params['kernel_size'],
      params['use_single_feature_prediction'], params['feature_flags'],
      params['use_CPU_only'], data_format)

  if mode == tf.estimator.ModeKeys.PREDICT:
    return tf.estimator.EstimatorSpec(mode=mode, predictions=predictions)
  
  targets = labels
  
  with tf.name_scope('loss_function'):
    with tf.name_scope('feature_loss'):
      training_features = params['training_features']
      for training_feature in training_features:
        training_feature.initialize(features, predictions, targets)
      feature_losses = []
      for training_feature in training_features:
        feature_losses.append(training_feature.loss())
      if len(feature_losses) > 0:
        feature_loss = tf.add_n(feature_losses)
      else:
        feature_loss = 0.0
    
    with tf.name_scope('combined_feature_loss'):
      combined_training_features = params['combined_training_features']
      if combined_training_features != None:
        for combined_training_feature in combined_training_features:
          combined_training_feature.initialize(features, predictions, targets)
        combined_feature_losses = []
        for combined_training_feature in combined_training_features:
          combined_feature_losses.append(combined_training_feature.loss())
        if len(combined_feature_losses) > 0:
          combined_feature_loss = tf.add_n(combined_feature_losses)
      else:
        combined_feature_loss = 0.0
    
    with tf.name_scope('combined_image_loss'):
      combined_image_training_feature = params['combined_image_training_feature']
      if combined_image_training_feature != None:
        combined_image_training_feature.initialize(features, predictions, targets)
        combined_image_feature_loss = combined_image_training_feature.loss()
      else:
        combined_image_feature_loss = 0.0
    
    # All losses combined
    loss = tf.add_n([feature_loss, combined_feature_loss, combined_image_feature_loss])

  
  # Configure the training op
  if mode == tf.estimator.ModeKeys.TRAIN:
    learning_rate = params['learning_rate']
    global_step = tf.train.get_or_create_global_step()
    first_decay_steps = 1000
    t_mul = 1.3 # Use t_mul more steps after each restart.
    m_mul = 0.8 # Multiply the learning rate after each restart with this number.
    alpha = 1 / 100. # Learning rate decays from 1 * learning_rate to alpha * learning_rate.
    learning_rate_decayed = tf.train.cosine_decay_restarts(
        learning_rate, global_step, first_decay_steps,
        t_mul=t_mul, m_mul=m_mul, alpha=alpha)
  
    tf.summary.scalar('learning_rate', learning_rate_decayed)
    tf.summary.scalar('batch_size', params['batch_size'])
    
    # Histograms
    for training_feature in training_features:
      training_feature.add_tracked_histograms()
    if combined_training_features != None:
      for combined_training_feature in combined_training_features:
        combined_training_feature.add_tracked_histograms()
    if combined_image_training_feature != None:
      combined_image_training_feature.add_tracked_histograms()
    
    # Summaries
    #with tf.name_scope('feature_summaries'):
    for training_feature in training_features:
      training_feature.add_tracked_summaries()
    #with tf.name_scope('combined_feature_summaries'):
    if combined_training_features != None:
      for combined_training_feature in combined_training_features:
        combined_training_feature.add_tracked_summaries()
    #with tf.name_scope('combined_summaries'):
    if combined_image_training_feature != None:
      combined_image_training_feature.add_tracked_summaries()
    
    with tf.name_scope('optimizer'):
      optimizer = tf.train.AdamOptimizer(learning_rate_decayed)
      train_op = optimizer.minimize(loss, global_step)
      eval_metric_ops = None
  else:
    train_op = None
    eval_metric_ops = {}

    #with tf.name_scope('features'):
    for training_feature in training_features:
      training_feature.add_tracked_metrics_to_dictionary(eval_metric_ops)
    
    #with tf.name_scope('combined_features'):
    if combined_training_features != None:
      for combined_training_feature in combined_training_features:
        combined_training_feature.add_tracked_metrics_to_dictionary(eval_metric_ops)
    
    #with tf.name_scope('combined'):
    if combined_image_training_feature != None:
      combined_image_training_feature.add_tracked_metrics_to_dictionary(eval_metric_ops)
    
  return tf.estimator.EstimatorSpec(
      mode=mode,
      loss=loss,
      train_op=train_op,
      eval_metric_ops=eval_metric_ops)


def input_fn_tfrecords(
    files, training_features_loader, training_features_augmentation, number_of_epochs, index_tuples, required_indices, rgb_permutation,
    tiles_height_width, batch_size, threads, data_format='channels_last', use_data_augmentation=False):
  
  def feature_parser(serialized_example):
    dataset = None
    
    # Load all the required indices.
    features = {}
    for training_feature_loader in training_features_loader:
      training_feature_loader.add_to_parse_dictionary(features, required_indices)
    
    parsed_features = tf.parse_single_example(serialized_example, features)
    
    for training_feature_loader in training_features_loader:
      training_feature_loader.deserialize(parsed_features, required_indices, tiles_height_width, tiles_height_width)
    
    # Prepare the examples.
    for index_tuple in index_tuples:
      sources = {}
      targets = {}
      for training_feature_loader in training_features_loader:
        training_feature_loader.add_to_sources_dictionary(sources, index_tuple)
        training_feature_loader.add_to_targets_dictionary(targets)
      
      if dataset == None:
        dataset = tf.data.Dataset.from_tensors((sources, targets))
      else:
        dataset = dataset.concatenate(tf.data.Dataset.from_tensors((sources, targets)))
    
    return dataset
  
  def data_augmentation(sources, targets):
    with tf.name_scope('data_augmentation'):
      for training_feature_augmentation in training_features_augmentation:
        training_feature_augmentation.intialize_from_dictionaries(sources, targets)
        
        flip = tf.random_uniform([1], minval=0, maxval=2, dtype=tf.int32)[0]
        if flip != 0:
          training_feature_augmentation.flip_left_right(data_format)
        
        rotate = tf.random_uniform([1], minval=0, maxval=4, dtype=tf.int32)[0]
        if rotate != 0:
          training_feature_augmentation.rotate90(rotate, data_format)

        if rgb_permutation != None:
          if rgb_permutation[0] != 0 and rgb_permutation[1] != 1 and rgb_permutation[2] != 2:
            training_feature_augmentation.permute_rgb(rgb_permutation, data_format)
    
        training_feature_augmentation.add_to_sources_dictionary(sources)
        training_feature_augmentation.add_to_targets_dictionary(targets)
    
    return sources, targets
  
  
  # REMARK: Due to stability issues, it was not possible to follow all the suggestions from the documentation like using the fused versions.
  
  shuffle_buffer_size = 10000
  files = files.repeat(number_of_epochs)
  files = files.shuffle(buffer_size=shuffle_buffer_size)
  
  dataset = tf.data.TFRecordDataset(files, compression_type='GZIP', buffer_size=None, num_parallel_reads=threads)
  dataset = dataset.flat_map(map_func=feature_parser)
  if use_data_augmentation:
    dataset = dataset.map(map_func=data_augmentation, num_parallel_calls=threads)
  
  shuffle_buffer_size = 20 * batch_size
  dataset = dataset.shuffle(buffer_size=shuffle_buffer_size)
  
  dataset = dataset.batch(batch_size)
  
  prefetch_buffer_size = 1
  dataset = dataset.prefetch(buffer_size=prefetch_buffer_size)
  
  iterator = dataset.make_one_shot_iterator()
  
  # `features` is a dictionary in which each value is a batch of values for
  # that feature; `target` is a batch of targets.
  features, targets = iterator.get_next()
  return features, targets


def train(
    tfrecords_directory, estimator, training_features_loader, training_features_augmentation,
    number_of_epochs, index_tuples, required_indices, rgb_permutation, tiles_height_width, batch_size, threads):
  
  files = tf.data.Dataset.list_files(tfrecords_directory + '/*')

  # Train the model
  use_data_augmentation = True
  estimator.train(input_fn=lambda: input_fn_tfrecords(
      files, training_features_loader, training_features_augmentation, number_of_epochs, index_tuples, required_indices, rgb_permutation,
      tiles_height_width, batch_size, threads, use_data_augmentation=use_data_augmentation))

def evaluate(
    tfrecords_directory, estimator, training_features_loader, training_features_augmentation,
    index_tuples, required_indices, rgb_permutation, tiles_height_width, batch_size, threads):
  
  files = tf.data.Dataset.list_files(tfrecords_directory + '/*')

  # Evaluate the model
  use_data_augmentation = True
  estimator.evaluate(input_fn=lambda: input_fn_tfrecords(
      files, training_features_loader, training_features_augmentation, 1, index_tuples, required_indices, rgb_permutation,
      tiles_height_width, batch_size, threads, use_data_augmentation=use_data_augmentation))

def source_index_tuples(number_of_sources_per_example, number_of_source_index_tuples, number_of_sources_per_target):
  if number_of_sources_per_example < number_of_sources_per_target:
    raise Exception('The source index tuples contain unique indices. That is not possible if there are fewer source examples than indices per tuple.')
  
  index_tuples = []
  if number_of_sources_per_target == 1:
    number_of_complete_tuple_sets = number_of_source_index_tuples // number_of_sources_per_example
    number_of_remaining_tuples = number_of_source_index_tuples % number_of_sources_per_example
    for _ in range(number_of_complete_tuple_sets):
      for index in range(number_of_sources_per_example):
        index_tuples.append([index])
    for _ in range(number_of_remaining_tuples):
      index = random.randint(0, number_of_sources_per_example - 1)
      index_tuples.append([index])
  else:
    for _ in range(number_of_source_index_tuples):
      tuple = []
      while len(tuple) < number_of_sources_per_target:
        index = random.randint(0, number_of_sources_per_example - 1)
        if not index in tuple:
          tuple.append(index)
      index_tuples.append(tuple)
  
  required_indices = []
  for tuple in index_tuples:
    for index in tuple:
      if not index in required_indices:
        required_indices.append(index)
  required_indices.sort()
  
  return index_tuples, required_indices

def rgb_color_permutation():
  rgb_permutation = [0, 1, 2]
  rgb_permutation = random.sample(rgb_permutation, len(rgb_permutation))
  return rgb_permutation
  

def main(parsed_arguments):
  if not isinstance(parsed_arguments.threads, int):
    parsed_arguments.threads = int(parsed_arguments.threads)

  try:
    json_filename = parsed_arguments.json_filename
    json_content = open(json_filename, 'r').read()
    parsed_json = json.loads(json_content)
  except:
    print('Expected a valid json file as argument.')
  
  model_directory = parsed_json['model_directory']
  base_tfrecords_directory = parsed_json['base_tfrecords_directory']
  modes = parsed_json['modes']
  
  number_of_source_index_tuples = parsed_json['number_of_source_index_tuples']
  number_of_sources_per_target = parsed_json['number_of_sources_per_target']
  use_rgb_permutations = parsed_json['use_rgb_permutations']
  
  loss_difference = parsed_json['loss_difference']
  loss_difference = LossDifferenceEnum[loss_difference]
  
  use_kernel_predicion = parsed_json['use_kernel_predicion']
  kernel_size = parsed_json['kernel_size']
  
  use_single_feature_prediction = parsed_json['use_single_feature_prediction']
  feature_flags = FeatureFlags(parsed_json['feature_flags'])
  
  features = parsed_json['features']
  combined_features = parsed_json['combined_features']
  combined_image = parsed_json['combined_image']
  
  # The names have to be sorted, otherwise the channels would be randomly mixed.
  feature_names = sorted(list(features.keys()))
  
  
  training_tfrecords_directory = os.path.join(base_tfrecords_directory, 'training')
  validation_tfrecords_directory = os.path.join(base_tfrecords_directory, 'validation')
  
  if not 'training' in modes:
    raise Exception('No training mode found.')
  if not 'validation' in modes:
    raise Exception('No validation mode found.')
  training_statistics_filename = os.path.join(base_tfrecords_directory, 'training.json')
  validation_statistics_filename = os.path.join(base_tfrecords_directory, 'validation.json')
  
  training_statistics_content = open(training_statistics_filename, 'r').read()
  training_statistics = json.loads(training_statistics_content)
  validation_statistics_content = open(validation_statistics_filename, 'r').read()
  validation_statistics = json.loads(validation_statistics_content)
  
  training_tiles_height_width = training_statistics['tiles_height_width']
  training_number_of_sources_per_example = training_statistics['number_of_sources_per_example']
  validation_tiles_height_width = validation_statistics['tiles_height_width']
  validation_number_of_sources_per_example = validation_statistics['number_of_sources_per_example']
  
  
  prediction_features = []
  for feature_name in feature_names:
    feature = features[feature_name]
    
    # REMARK: It is assumed that there are no features which are only a target, without also being a source.
    if feature['is_source']:
      preserve_source = use_kernel_predicion
      feature_variance = feature['feature_variance']
      feature_variance = FeatureVariance(
          feature_variance['use_variance'], feature_variance['relative_variance'],
          feature_variance['compute_before_standardization'], feature_variance['compress_to_one_channel'],
          feature_name)
      feature_standardization = feature['standardization']
      feature_standardization = FeatureStandardization(
          feature_standardization['use_log1p'], feature_standardization['mean'], feature_standardization['variance'],
          feature_name)      
      prediction_feature = PredictionFeature(
          number_of_sources_per_target, preserve_source, feature['is_target'], feature_standardization, feature_variance,
          feature['feature_flags'], feature['number_of_channels'], feature_name)
      prediction_features.append(prediction_feature)
  
  if use_single_feature_prediction:
    for prediction_feature in prediction_features:
      if prediction_feature.is_target:
        feature_flags.add_render_pass_name_to_feature_flag_names(prediction_feature.name, prediction_feature.feature_flags)
    feature_flags.freeze()
  
  # Training features.

  training_features = []
  feature_name_to_training_feature = {}
  for feature_name in feature_names:
    feature = features[feature_name]
    if feature['is_source'] and feature['is_target']:
      
      # Training loss
      
      loss_weights = feature['loss_weights']
      if 'loss_weights_masked' in feature:
        loss_weights_masked = feature['loss_weights_masked']
      else:
        loss_weights_masked = {}
        loss_weights_masked['mean'] = 0.0
        loss_weights_masked['variation'] = 0.0
        loss_weights_masked['ms_ssim'] = 0.0
      
      
      # Training metrics
      
      statistics = feature['statistics']
      if 'statistics_masked' in feature:
        statistics_masked = feature['statistics_masked']
      else:
        statistics_masked = {}
        statistics_masked['track_mean'] = False
        statistics_masked['track_variation'] = False
        statistics_masked['track_ms_ssim'] = False
        statistics_masked['track_difference_histogram'] = False
        statistics_masked['track_variation_difference_histogram'] = False
        
      training_feature = TrainingFeature(
          feature_name, loss_difference,
          loss_weights['mean'], loss_weights['variation'], loss_weights['ms_ssim'],
          loss_weights_masked['mean'], loss_weights_masked['variation'], loss_weights_masked['ms_ssim'],
          statistics['track_mean'], statistics['track_variation'], statistics['track_ms_ssim'],
          statistics['track_difference_histogram'], statistics['track_variation_difference_histogram'],
          statistics_masked['track_mean'], statistics_masked['track_variation'], statistics_masked['track_ms_ssim'],
          statistics_masked['track_difference_histogram'], statistics_masked['track_variation_difference_histogram'])
      training_features.append(training_feature)
      feature_name_to_training_feature[feature_name] = training_feature

  training_features_loader = []
  training_features_augmentation = []
  for prediction_feature in prediction_features:
    training_features_loader.append(TrainingFeatureLoader(
        prediction_feature.is_target, prediction_feature.number_of_channels, prediction_feature.name))
    training_features_augmentation.append(TrainingFeatureAugmentation(
        number_of_sources_per_target, prediction_feature.is_target,
        prediction_feature.number_of_channels, prediction_feature.name))

  
  # Combined training features.
  
  combined_training_features = []
  combined_feature_name_to_combined_training_feature = {}
  combined_feature_names = list(combined_features.keys())
  for combined_feature_name in combined_feature_names:
    combined_feature = combined_features[combined_feature_name]
    statistics = combined_feature['statistics']
    loss_weights = combined_feature['loss_weights']
    if loss_weights['mean'] > 0. or loss_weights['variation'] > 0:
      color_feature_name = RenderPasses.combined_to_color_render_pass(combined_feature_name)
      direct_feature_name = RenderPasses.combined_to_direct_render_pass(combined_feature_name)
      indirect_feature_name = RenderPasses.combined_to_indirect_render_pass(combined_feature_name)
      combined_training_feature = CombinedTrainingFeature(
          combined_feature_name, loss_difference,
          feature_name_to_training_feature[color_feature_name],
          feature_name_to_training_feature[direct_feature_name],
          feature_name_to_training_feature[indirect_feature_name],
          loss_weights['mean'], loss_weights['variation'], loss_weights['ms_ssim'],
          0.0, 0.0, 0.0,
          statistics['track_mean'], statistics['track_variation'], statistics['track_ms_ssim'],
          statistics['track_difference_histogram'], statistics['track_variation_difference_histogram'],
          0.0, 0.0, 0.0,
          0.0, 0.0)
      combined_training_features.append(combined_training_feature)
      combined_feature_name_to_combined_training_feature[combined_feature_name] = combined_training_feature
        
  if len(combined_training_features) == 0:
    combined_training_features = None
  
  
  # Combined image training feature.
  
  combined_image_training_feature = None
  statistics = combined_image['statistics']
  loss_weights = combined_image['loss_weights']
  if loss_weights['mean'] > 0. or loss_weights['variation'] > 0:
    combined_image_training_feature = CombinedImageTrainingFeature(
        RenderPasses.COMBINED, loss_difference,
        combined_feature_name_to_combined_training_feature['Diffuse'],
        combined_feature_name_to_combined_training_feature['Glossy'],
        combined_feature_name_to_combined_training_feature['Subsurface'],
        combined_feature_name_to_combined_training_feature['Transmission'],
        feature_name_to_training_feature[RenderPasses.EMISSION],
        feature_name_to_training_feature[RenderPasses.ENVIRONMENT],
        loss_weights['mean'], loss_weights['variation'], loss_weights['ms_ssim'],
        0.0, 0.0, 0.0,
        statistics['track_mean'], statistics['track_variation'], statistics['track_ms_ssim'],
        statistics['track_difference_histogram'], statistics['track_variation_difference_histogram'],
        0.0, 0.0, 0.0,
        0.0, 0.0)
  
  
  # TODO: CPU only has to be configurable. (DeepBlender)
  # TODO: Learning rate has to be configurable. (DeepBlender)
  
  learning_rate = 1e-20
  use_XLA = True
  use_CPU_only = False
  
  run_config = None
  if use_XLA:
    if use_CPU_only:
      session_config = tf.ConfigProto(device_count = {'GPU': 0})
    else:
      session_config = tf.ConfigProto()
    session_config.graph_options.optimizer_options.global_jit_level = tf.OptimizerOptions.ON_1
    save_summary_steps = 100
    save_checkpoints_step = 500
    run_config = tf.estimator.RunConfig(
        session_config=session_config, save_summary_steps=save_summary_steps,
        save_checkpoints_steps=save_checkpoints_step)
  
  estimator = tf.estimator.Estimator(
      model_fn=model_fn,
      model_dir=model_directory,
      config=run_config,
      params={
          'prediction_features': prediction_features,
          'use_CPU_only': use_CPU_only,
          'data_format': parsed_arguments.data_format,
          'learning_rate': learning_rate,
          'batch_size': parsed_arguments.batch_size,
          'use_kernel_predicion': use_kernel_predicion,
          'kernel_size': kernel_size,
          'use_single_feature_prediction': use_single_feature_prediction,
          'feature_flags': feature_flags,
          'training_features': training_features,
          'combined_training_features': combined_training_features,
          'combined_image_training_feature': combined_image_training_feature})
  
  if parsed_arguments.validate:
    index_tuples, required_indices = source_index_tuples(
        validation_number_of_sources_per_example, number_of_source_index_tuples, number_of_sources_per_target)
    rgb_permutation = None
    if use_rgb_permutations:
      rgb_permutation = rgb_color_permutation()
    evaluate(validation_tfrecords_directory, estimator, training_features_loader, training_features_augmentation,
        index_tuples, required_indices, rgb_permutation, training_tiles_height_width,
        parsed_arguments.batch_size, parsed_arguments.threads)
  else:
    remaining_number_of_epochs = parsed_arguments.train_epochs
    while remaining_number_of_epochs > 0:
      number_of_training_epochs = parsed_arguments.validation_interval
      if remaining_number_of_epochs < number_of_training_epochs:
        number_of_training_epochs = remaining_number_of_epochs
      
      for _ in range(number_of_training_epochs):
        epochs_to_train = 1
        index_tuples, required_indices = source_index_tuples(
            training_number_of_sources_per_example, number_of_source_index_tuples, number_of_sources_per_target)
        rgb_permutation = None
        if use_rgb_permutations:
          rgb_permutation = rgb_color_permutation()
        train(
            training_tfrecords_directory, estimator, training_features_loader, training_features_augmentation,
            epochs_to_train, index_tuples, required_indices, rgb_permutation, training_tiles_height_width,
            parsed_arguments.batch_size, parsed_arguments.threads)
      
      index_tuples, required_indices = source_index_tuples(
          validation_number_of_sources_per_example, number_of_source_index_tuples, number_of_sources_per_target)
      rgb_permutation = None
      if use_rgb_permutations:
        rgb_permutation = rgb_color_permutation()
      evaluate(validation_tfrecords_directory, estimator, training_features_loader, training_features_augmentation,
          index_tuples, required_indices, rgb_permutation, training_tiles_height_width,
          parsed_arguments.batch_size, parsed_arguments.threads)
      
      remaining_number_of_epochs = remaining_number_of_epochs - number_of_training_epochs


if __name__ == '__main__':
  parsed_arguments, unparsed = parser.parse_known_args()
  main(parsed_arguments)