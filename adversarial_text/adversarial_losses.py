# Copyright 2017 Google, Inc. All Rights Reserved.
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
# ==============================================================================

"""Adversarial losses for text models."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

flags = tf.app.flags
FLAGS = flags.FLAGS

# Adversarial and virtual adversarial training parameters.
flags.DEFINE_float('perturb_norm_length', 0.1,
                   'Norm length of adversarial perturbation to be '
                   'optimized with validation')

# Virtual adversarial training parameters
flags.DEFINE_integer('num_power_iteration', 1, 'The number of power iteration')
flags.DEFINE_float('small_constant_for_finite_diff', 1e-3,
                   'Small constant for finite difference method')

# Parameters for building the graph
flags.DEFINE_string('adv_training_method', None,
                    'The flag which specifies training method. '
                    '"rp"  : random perturbation training '
                    '"at"  : adversarial training '
                    '"vat" : virtual adversarial training '
                    '"atvat" : at + vat ')
flags.DEFINE_float('adv_reg_coeff', 1.0,
                   'Regularization coefficient of adversarial loss.')


def random_perturbation_loss(embedded, length, loss_fn):
  """Adds noise to embeddings and recomputes classification loss."""
  noise = tf.random_normal(shape=tf.shape(embedded))
  perturb = _scale_l2(_mask_by_length(noise, length), FLAGS.perturb_norm_length)
  return loss_fn(embedded + perturb)


def adversarial_loss(embedded, loss, loss_fn):
  """Adds gradient to embedding and recomputes classification loss."""
  grad, = tf.gradients(
      loss,
      embedded,
      aggregation_method=tf.AggregationMethod.EXPERIMENTAL_ACCUMULATE_N)
  grad = tf.stop_gradient(grad)
  perturb = _scale_l2(grad, FLAGS.perturb_norm_length)
  return loss_fn(embedded + perturb)


def virtual_adversarial_loss(logits, embedded, inputs,
                             logits_from_embedding_fn):
  """Virtual adversarial loss.

  Computes virtual adversarial perturbation by finite difference method and
  power iteration, adds it to the embedding, and computes the KL divergence
  between the new logits and the original logits.

  Args:
    logits: 2-D float Tensor, [num_timesteps*batch_size, m], where m=1 if
      num_classes=2, otherwise m=num_classes.
    embedded: 3-D float Tensor, [batch_size, num_timesteps, embedding_dim].
    inputs: VatxtInput.
    logits_from_embedding_fn: callable that takes embeddings and returns
      classifier logits.

  Returns:
    kl: float scalar.
  """
  # Stop gradient of logits. See https://arxiv.org/abs/1507.00677 for details.
  logits = tf.stop_gradient(logits)
  # Only care about the KL divergence on the final timestep.
  weights = _end_of_seq_mask(inputs.labels)

  # Initialize perturbation with random noise.
  # shape(embedded) = (batch_size, num_timesteps, embedding_dim)
  d = _mask_by_length(tf.random_normal(shape=tf.shape(embedded)), inputs.length)

  # Perform finite difference method and power iteration.
  # See Eq.(8) in the paper http://arxiv.org/pdf/1507.00677.pdf,
  # Adding small noise to input and taking gradient with respect to the noise
  # corresponds to 1 power iteration.
  for _ in xrange(FLAGS.num_power_iteration):
    d = _scale_l2(d, FLAGS.small_constant_for_finite_diff)
    d_logits = logits_from_embedding_fn(embedded + d)
    kl = _kl_divergence_with_logits(logits, d_logits, weights)
    d, = tf.gradients(
        kl,
        d,
        aggregation_method=tf.AggregationMethod.EXPERIMENTAL_ACCUMULATE_N)
    d = tf.stop_gradient(d)

  perturb = _scale_l2(
      _mask_by_length(d, inputs.length), FLAGS.perturb_norm_length)
  vadv_logits = logits_from_embedding_fn(embedded + perturb)
  return _kl_divergence_with_logits(logits, vadv_logits, weights)


def random_perturbation_loss_bidir(embedded, length, loss_fn):
  """Adds noise to embeddings and recomputes classification loss."""
  noise = [tf.random_normal(shape=tf.shape(emb)) for emb in embedded]
  masked = [_mask_by_length(n, length) for n in noise]
  scaled = [_scale_l2(m, FLAGS.perturb_norm_length) for m in masked]
  return loss_fn([e + s for (e, s) in zip(embedded, scaled)])


def adversarial_loss_bidir(embedded, loss, loss_fn):
  """Adds gradient to embeddings and recomputes classification loss."""
  grads = tf.gradients(
      loss,
      embedded,
      aggregation_method=tf.AggregationMethod.EXPERIMENTAL_ACCUMULATE_N)
  adv_exs = [
      emb + _scale_l2(tf.stop_gradient(g), FLAGS.perturb_norm_length)
      for emb, g in zip(embedded, grads)
  ]
  return loss_fn(adv_exs)


def virtual_adversarial_loss_bidir(logits, embedded, inputs,
                                   logits_from_embedding_fn):
  """Virtual adversarial loss for bidirectional models."""
  logits = tf.stop_gradient(logits)
  f_inputs, _ = inputs
  weights = _end_of_seq_mask(f_inputs.labels)

  perturbs = [
      _mask_by_length(tf.random_normal(shape=tf.shape(emb)), f_inputs.length)
      for emb in embedded
  ]
  for _ in xrange(FLAGS.num_power_iteration):
    perturbs = [
        _scale_l2(d, FLAGS.small_constant_for_finite_diff) for d in perturbs
    ]
    d_logits = logits_from_embedding_fn(
        [emb + d for (emb, d) in zip(embedded, perturbs)])
    kl = _kl_divergence_with_logits(logits, d_logits, weights)
    perturbs = tf.gradients(
        kl,
        perturbs,
        aggregation_method=tf.AggregationMethod.EXPERIMENTAL_ACCUMULATE_N)
    perturbs = [tf.stop_gradient(d) for d in perturbs]

  perturbs = [
      _scale_l2(_mask_by_length(d, f_inputs.length), FLAGS.perturb_norm_length)
      for d in perturbs
  ]
  vadv_logits = logits_from_embedding_fn(
      [emb + d for (emb, d) in zip(embedded, perturbs)])
  return _kl_divergence_with_logits(logits, vadv_logits, weights)


def _mask_by_length(t, length):
  """Mask t, 3-D [batch, time, dim], by length, 1-D [batch,]."""
  maxlen = t.get_shape().as_list()[1]
  mask = tf.sequence_mask(length, maxlen=maxlen)
  mask = tf.expand_dims(tf.cast(mask, tf.float32), -1)
  # shape(mask) = (batch, num_timesteps, 1)
  return t * mask


def _scale_l2(x, norm_length):
  # shape(x) = (batch, num_timesteps, d)

  # Divide x by max(abs(x)) for a numerically stable L2 norm.
  # 2norm(x) = a * 2norm(x/a)
  # Scale over the full sequence, dims (1, 2)
  alpha = tf.reduce_max(tf.abs(x), (1, 2), keep_dims=True) + 1e-12
  l2_norm = alpha * tf.sqrt(tf.reduce_sum(tf.pow(x / alpha, 2), (1, 2),
                                          keep_dims=True) + 1e-6)
  x_unit = x / l2_norm
  return norm_length * x_unit


def _end_of_seq_mask(tokens):
  """Generate a mask for the EOS token (1.0 on EOS, 0.0 otherwise).

  Args:
    tokens: 1-D integer tensor [num_timesteps*batch_size]. Each element is an
      id from the vocab.

  Returns:
    Float tensor same shape as tokens, whose values are 1.0 on the end of
    sequence and 0.0 on the others.
  """
  eos_id = FLAGS.vocab_size - 1
  return tf.cast(tf.equal(tokens, eos_id), tf.float32)


def _kl_divergence_with_logits(q_logits, p_logits, weights):
  """Returns weighted KL divergence between distributions q and p.

  Args:
    q_logits: logits for 1st argument of KL divergence shape
              [num_timesteps * batch_size, num_classes] if num_classes > 2, and
              [num_timesteps * batch_size] if num_classes == 2.
    p_logits: logits for 2nd argument of KL divergence with same shape q_logits.
    weights: 1-D float tensor with shape [num_timesteps * batch_size].
             Elements should be 1.0 only on end of sequences

  Returns:
    KL: float scalar.
  """
  # For logistic regression
  if FLAGS.num_classes == 2:
    q = tf.nn.sigmoid(q_logits)
    p = tf.nn.sigmoid(p_logits)
    kl = (-tf.nn.sigmoid_cross_entropy_with_logits(logits=q_logits, labels=q) +
          tf.nn.sigmoid_cross_entropy_with_logits(logits=p_logits, labels=q))

  # For softmax regression
  else:
    q = tf.nn.softmax(q_logits)
    p = tf.nn.softmax(p_logits)
    kl = tf.reduce_sum(q * (tf.log(q) - tf.log(p)), 1)

  num_labels = tf.reduce_sum(weights)
  num_labels = tf.where(tf.equal(num_labels, 0.), 1., num_labels)

  kl.get_shape().assert_has_rank(2)
  weights.get_shape().assert_has_rank(1)
  loss = tf.identity(tf.reduce_sum(tf.expand_dims(weights, -1) * kl) /
                     num_labels, name='kl')
  return loss
