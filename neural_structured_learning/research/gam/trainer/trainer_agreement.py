# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Trainer for the agreement model in Graph Agreement Models without a graph.

This class contains functionality that allows for training an agreement model
to be used as part of Graph Agreement Models.
This implementation does not use a provided graph, but samples random pairs
of samples.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import logging
import os

from gam.trainer import batch_iterator
from gam.trainer import Trainer

import numpy as np
import tensorflow as tf


def accuracy_binary(normalized_preds, labels, threshold=0.5):
  """Accuracy with probabilities for binary classification."""
  predictions = tf.greater_equal(normalized_preds, threshold)
  labels = tf.cast(labels, tf.bool)
  accuracy = tf.cast(tf.equal(predictions, labels), tf.float32)
  return tf.reduce_mean(accuracy)


class TrainerAgreement(Trainer):
  """Trainer for the agreement model.

  Attributes:
    model: A Model used to decide if two samples should have the same label.
    is_train: A placeholder for a boolean value specyfing if the model is used
      for train or evaluation.
    data: A CotrainDataset object.
    optimizer: Optimizer used for training the agreement model.
    min_num_iter: An integer representing the minimum number of iterations to
      train the agreement model.
    max_num_iter: An integer representing the maximum number of iterations to
      train the agreement model.
    num_iter_after_best_val: An integer representing the number of extra
      iterations to perform after improving the validation set accuracy.
    max_num_iter_cotrain: An integer representing the maximum number of cotrain
      iterations to train for.
    num_warm_up_iter: The agreement model will return 0 for the first
      `num_warm_up_iter` co-training iterations.
    batch_size: Batch size for used when training and evaluating the agreement
      model.
    gradient_clip: A float number representing the maximum gradient norm allowed
      if we do gradient clipping. If None, no gradient clipping is performed.
    enable_summaries: Boolean specifying whether to enable variable summaries.
    summary_step: Integer representing the summary step size.
    summary_dir: String representing the path to a directory where to save the
      variable summaries.
    logging_step: Integer representing the number of iterations after which
      we log the loss of the model.
    eval_step: Integer representing the number of iterations after which we
      evaluate the model.
    abs_loss_chg_tol: A float representing the absolute tolerance for checking
      if the training loss has converged. If the difference between the current
      loss and previous loss is less than `abs_loss_chg_tol`, we count this
      iteration towards convergence (see `loss_chg_iter_below_tol`).
    rel_loss_chg_tol: A float representing the relative tolerance for checking
      if the training loss has converged. If the ratio between the current loss
      and previous loss is less than `rel_loss_chg_tol`, we count this iteration
      towards convergence (see `loss_chg_iter_below_tol`).
    loss_chg_iter_below_tol: An integer representing the number of consecutive
      iterations that pass the convergence criteria before stopping training.
    warm_start: Whether the agreement model parameters are initialized at their
      best value in the previous cotrain iteration. If False, they are
      reinitialized.
    checkpoints_dir: Path to the folder where to store TensorFlow model
      checkpoints.
    weight_decay: Weight decay value.
    weight_decay_schedule: Schedule for the weight decay variable.
    num_pairs_eval_random: Integer representing the number of pairs to use
      for evaluation. These pairs are randomly drawn from all datasets,
      including validation and test. This is only used for monitoring the
      performance, but is not involved in training the agreement model.
    agree_by_default: Boolean specifying whether to return agreement by default
      or disagreement by default when the agreement model is not warmed up.
    percent_val: Ratio of samples to use for validation.
    max_num_samples_val: Maximum number of samples to include in the validation
      set.
    seed: Integer representing the seed for the random number generator.
  """

  def __init__(self,
               model,
               is_train,
               data,
               optimizer,
               lr_initial,
               min_num_iter,
               max_num_iter,
               num_iter_after_best_val,
               max_num_iter_cotrain,
               num_warm_up_iter,
               batch_size,
               gradient_clip=None,
               enable_summaries=False,
               summary_step=1,
               summary_dir=None,
               logging_step=1,
               eval_step=1,
               abs_loss_chg_tol=1e-10,
               rel_loss_chg_tol=1e-7,
               loss_chg_iter_below_tol=20,
               warm_start=False,
               checkpoints_dir=None,
               weight_decay=None,
               weight_decay_schedule=None,
               num_pairs_eval_random=1000,
               agree_by_default=False,
               percent_val=0.2,
               max_num_samples_val=10000,
               seed=None,
               lr_decay_steps=None,
               lr_decay_rate=None):
    super(TrainerAgreement, self).__init__(
        model=model,
        abs_loss_chg_tol=abs_loss_chg_tol,
        rel_loss_chg_tol=rel_loss_chg_tol,
        loss_chg_iter_below_tol=loss_chg_iter_below_tol)
    self.data = data
    self.optimizer = optimizer
    self.min_num_iter = min_num_iter
    self.max_num_iter = max_num_iter
    self.num_iter_after_best_val = num_iter_after_best_val
    self.max_num_iter_cotrain = max_num_iter_cotrain
    self.num_warm_up_iter = num_warm_up_iter
    self.batch_size = batch_size
    self.gradient_clip = gradient_clip
    self.enable_summaries = enable_summaries
    self.summary_step = summary_step
    self.summary_dir = summary_dir
    self.checkpoints_dir = checkpoints_dir
    self.logging_step = logging_step
    self.eval_step = eval_step
    self.num_iter_trained = 0
    self.warm_start = warm_start
    self.checkpoint_path = (os.path.join(checkpoints_dir, 'agree_best.ckpt')
                            if checkpoints_dir is not None else None)
    self.weight_decay = weight_decay
    self.weight_decay_schedule = weight_decay_schedule
    self.num_pairs_eval_random = num_pairs_eval_random
    self.agree_by_default = agree_by_default
    self.percent_val = percent_val
    self.max_num_samples_val = max_num_samples_val
    self.original_var_scope = None
    self.lr_initial = lr_initial
    self.lr_decay_steps = lr_decay_steps
    self.lr_decay_rate = lr_decay_rate

    # Build TensorFlow graph.
    logging.info('Building TensorFlow agreement graph...')
    # The agreement model computes the label agreement between two samples.
    # We will refer to these samples as the src and tgt sample, using
    # graph terminology.

    # Create placeholders, and assign to these variables by default.
    features_shape = [None] + list(data.features_shape)
    src_features = tf.placeholder(
        tf.float32, shape=features_shape, name='src_features')
    tgt_features = tf.placeholder(
        tf.float32, shape=features_shape, name='tgt_features')
    # Create a placeholder for the agreement labels.
    labels = tf.placeholder(tf.float32, shape=(None,), name='labels')

    # Create variables and predictions.
    predictions, normalized_predictions, variables, reg_params = (
        self.create_agreement_prediction(src_features, tgt_features, is_train))

    # Create a variable for weight decay that may be updated later.
    weight_decay_var, weight_decay_update = self._create_weight_decay_var(
        weight_decay, weight_decay_schedule)

    # Create counter for the total number of agreement train iterations.
    iter_agr_total, iter_agr_total_update = self._create_counter()

    # Create loss.
    loss_op = self.model.get_loss(
        predictions=predictions,
        targets=labels,
        reg_params=reg_params,
        weight_decay=weight_decay_var)

    # Create accuracy.
    accuracy = accuracy_binary(normalized_predictions, labels)

    # Create optimizer.
    self.global_step = tf.train.get_or_create_global_step()
    if self.lr_decay_steps is not None and self.lr_decay_rate is not None:
      self.lr = tf.train.exponential_decay(
          self.lr_initial,
          self.global_step,
          self.lr_decay_steps,
          self.lr_decay_rate,
          staircase=True)
      self.optimizer = optimizer(self.lr)
    else:
      self.optimizer = optimizer(lr_initial)

    # Create train op.
    grads_and_vars = self.optimizer.compute_gradients(
        loss_op,
        tf.trainable_variables())
    # Clip gradients.
    if self.gradient_clip:
      variab = [elem[1] for elem in grads_and_vars]
      gradients = [elem[0] for elem in grads_and_vars]
      gradients, _ = tf.clip_by_global_norm(gradients, self.gradient_clip)
      grads_and_vars = zip(gradients, variab)
    train_op = self.optimizer.apply_gradients(
        grads_and_vars, global_step=self.global_step)

    # Create Tensorboard summaries.
    if self.enable_summaries:
      summaries = [tf.summary.scalar('loss_agreement_inner', loss_op)]
      self.summary_op = tf.summary.merge(summaries)

    # Create a saver for the model trainable variables.
    trainable_vars = [v for _, v in grads_and_vars]
    saver = tf.train.Saver(trainable_vars)

    # Put together all variables that need to be saved in case the process is
    # interrupted and needs to be restarted.
    self.vars_to_save = [weight_decay_var, iter_agr_total]
    if self.warm_start:
      self.vars_to_save.extend([v for v in variables])

    # More variables to be initialized after the session is created.
    self.is_initialized = False

    self.trainable_vars = trainable_vars
    self.rng = np.random.RandomState(seed)
    self.src_features = src_features
    self.tgt_features = tgt_features
    self.labels = labels
    self.predictions = predictions
    self.normalized_predictions = normalized_predictions
    self.variables = variables
    self.reg_params = reg_params
    self.weight_decay_var = weight_decay_var
    self.weight_decay_update = weight_decay_update
    self.iter_agr_total = iter_agr_total
    self.iter_agr_total_update = iter_agr_total_update
    self.accuracy = accuracy
    self.train_op = train_op
    self.loss_op = loss_op
    self.saver = saver
    self.batch_size_actual = tf.shape(self.predictions)[0]
    self.reset_optimizer = tf.variables_initializer(self.optimizer.variables())
    self.is_train = is_train

  def create_agreement_prediction(self, src_features, tgt_features, is_train,
                                  **unused_kwargs):
    """Creates the agreement prediction TensorFlow subgraph.

    This function is intended to be used both from inside TrainerAgreement for
    training an agreement model, but also from the TrainerClassification class
    when creating the agreement loss term.

    Arguments:
      src_features: A Tensor or Placeholder of shape (batch_size, num_features)
        containing the features of the source sample of an edge.
      tgt_features: A Tensor or Placeholder of shape (batch_size, num_features)
        containing the features of the target sample of an edge.
      is_train: A boolean Placeholder specifying if this a train or test regime.
      unused_kwargs: Other unused keyword arguments, which we allow in order to
        create a common interface with TrainerPerfectAgreement.
    Returns:
      predictions: A Tensor of shape (batch_size,) containing the agreement
        prediction logits.
      normalized_predictions: A Tensor of shape (batch_size,) with values in
        [0, 1], containing the agreement prediction probabilities.
      variables: A dictionary of trainable variables mapping from a string name'
        to a TensorFlow variable.
      reg_params: A dictionary of variables that are used in the regularization
        weight decay term. It maps from a string name to a TensorFlow variable.
    """
    # The following lines are a trick that allows to reuse the same agreement
    # computation TensorFlow graph both from TrainerAgreement and from outside
    # it (e.g. from TrainerClassification). In order to reuse the graph, we
    # need to force the same variable scope, no matter where this function is
    # called from, and to enable variable reuse after the first time it is
    # called.
    reuse = True
    if self.original_var_scope is None:
      self.original_var_scope = tf.get_variable_scope()
      reuse = tf.AUTO_REUSE
    with tf.variable_scope(self.original_var_scope, auxiliary_name_scope=False,
                           reuse=reuse):
      # Create variables and predictions.
      # Can replace the encoding step once there is a shared
      # encoding between the classification and agreement model.
      encoding, variables, reg_params = self.model.get_encoding_and_params(
          inputs=(src_features, tgt_features), is_train=is_train)
      predictions, variables_pred, reg_params_pred = (
          self.model.get_predictions_and_params(encoding=encoding,
                                                is_train=is_train))
      variables.update(variables_pred)
      reg_params.update(reg_params_pred)
      normalized_predictions = self.model.normalize_predictions(predictions)
      return predictions, normalized_predictions, variables, reg_params

  def _create_weight_decay_var(self, weight_decay_initial,
                               weight_decay_schedule):
    """Creates a weight decay variable that can be updated using a schedule."""
    weight_decay_var = None
    weight_decay_update = None
    if weight_decay_schedule is None:
      weight_decay_var = tf.constant(
          weight_decay_initial, dtype=tf.float32, name='weight_decay')
    elif weight_decay_schedule == 'linear':
      weight_decay_var = tf.get_variable(
          name='weight_decay',
          initializer=tf.constant(
              weight_decay_initial, name='weight_decay_initial'),
          use_resource=True,
          trainable=False)
      update_rate = weight_decay_initial / float(self.max_num_iter_cotrain)
      weight_decay_update = weight_decay_var.assign(weight_decay_var -
                                                    update_rate)
    else:
      return NotImplementedError(
          'Schedule %s is not implemented for the weight decay variable.' %
          str(weight_decay_schedule))
    return weight_decay_var, weight_decay_update

  def _create_counter(self):
    """Creates a cummulative iteration counter for all agreement steps."""
    iter_agr_total = tf.get_variable(
        name='iter_agr_total',
        initializer=tf.constant(0, name='iter_agr_total'),
        use_resource=True,
        trainable=False)
    iter_agr_total_update = iter_agr_total.assign_add(1)
    return iter_agr_total, iter_agr_total_update

  def _construct_feed_dict(self, data_iterator, is_train):
    """Construct feed dictionary containing features and labels."""
    try:
      neighbors, agreement_labels = next(data_iterator)
      src_features = self.data.get_features(neighbors[:, 0])
      tgt_features = self.data.get_features(neighbors[:, 1])
      feed_dict = {
          self.src_features: src_features,
          self.tgt_features: tgt_features,
          self.labels: agreement_labels,
          self.is_train: is_train
      }
      return feed_dict
    except StopIteration:
      # If the iterator has finished, return None.
      return None

  def _eval_random_pairs(self, data, session):
    """Evaluate on random pairs of nodes, and estimate accuracy.

    We do this to get an estimate of how well the agreement model is doing
    with respect to the true labels. This is for monitoring only, and is not
    used when training.

    Arguments:
      data: A CotrainDataset object.
      session: A TensorFlow session.

    Returns:
      acc: Total accuracy on random pairs of samples.
    """
    # Select at random num_pairs_eval_random pairs of nodes.
    src_indices = self.rng.random_integers(
        0, data.num_samples - 1, (self.num_pairs_eval_random,))
    tgt_indices = self.rng.random_integers(
        0, data.num_samples - 1, (self.num_pairs_eval_random,))
    src_features = data.get_features(src_indices)
    tgt_features = data.get_features(tgt_indices)
    src_labels = data.get_original_labels(src_indices)
    tgt_labels = data.get_original_labels(tgt_indices)
    agreement_labels = src_labels == tgt_labels
    feed_dict = {
        self.src_features: src_features,
        self.tgt_features: tgt_features,
        self.labels: agreement_labels.astype(np.float32)}
    # Evaluate agreement.
    acc = session.run(self.accuracy, feed_dict=feed_dict)
    return acc

  def _train_iterator(self, labeled_samples, neighbors_val, data,
                      ratio_pos_to_neg=None):
    """An iterator over pairs of samples for training the agreement model.

    Provides batches of node pairs, including their features and the agreement
    label (i.e. whether their labels agree). A set of validation pairs
    is also provided to make sure those samples are not included in train.

    Arguments:
      labeled_samples: An array of integers representing the indices of the
        labeled nodes.
      neighbors_val: An array of shape (num_samples, 2), where each row
        represents a pair of sample indices used for validation.
      data: A Dataset object used to provided the labels of the labeled samples.
      ratio_pos_to_neg: A float representing the ratio of positive to negative
        samples in the training set. If this is provided, the train iterator
        will do rejection sampling based on this ratio to keep the training
        data balanced. If None, we sample uniformly.
    Yields:
      neighbors_batch: An array of shape (batch_size, 2), where each row
        represents a pair of sample indices used for training. It will not
        include pairs of samples that are in the provided neighbors_val.
      agreement_batch: An array of shape (batch_size,) with binary values,
        where each row represents whether the labels of the corresponding
        neighbor pair agree (1.0) or not (0.0).
    """
    neighbors_val = set([(pair[0], pair[1]) if pair[0] < pair[1] else
                         (pair[1], pair[0]) for pair in neighbors_val])
    neighbors_batch = np.empty(shape=(self.batch_size, 2), dtype=np.int32)
    agreement_batch = np.empty(shape=(self.batch_size,), dtype=np.float32)
    # For sampling random pairs of samples very fast, we create two buffers,
    # one containing elements for the left side of the pair, the other for the
    # right side, and we go through them in parallel.
    buffer_left = np.copy(labeled_samples)
    buffer_right = np.copy(labeled_samples)
    idx_buffer = np.inf
    num_labeled = len(labeled_samples)
    while True:
      num_added = 0
      while num_added < self.batch_size:
        if idx_buffer >= num_labeled:
          idx_buffer = 0
          self.rng.shuffle(buffer_left)
          self.rng.shuffle(buffer_right)
        pair = (buffer_left[idx_buffer], buffer_right[idx_buffer])
        idx_buffer += 1
        if pair[0] == pair[1]:
          continue
        ordered_pair = ((pair[0], pair[1]) if pair[0] < pair[1] else
                        (pair[1], pair[0]))
        if ordered_pair in neighbors_val:
          continue
        agreement = data.get_labels(pair[0]) == data.get_labels(pair[1])
        if ratio_pos_to_neg is not None:
          # To keep the positive and negatives balanced, do rejection sampling
          # according to their ratio.
          if ratio_pos_to_neg < 1 and not agreement:
            # Reject a negative sample with some probability.
            random_number = self.rng.rand(1)[0]
            if random_number > ratio_pos_to_neg:
              continue
          elif ratio_pos_to_neg > 1 and agreement:
            # Reject a positive sample with some probability.
            random_number = self.rng.random()
            if random_number > 1.0 / ratio_pos_to_neg:
              continue
        neighbors_batch[num_added][0] = pair[0]
        neighbors_batch[num_added][1] = pair[1]
        agreement_batch[num_added] = agreement
        num_added += 1
      yield neighbors_batch, agreement_batch

  def _select_val_set(self, labeled_samples, num_samples, data,
                      ratio_pos_to_neg=None):
    """Select a validation set for the agreement model.

    This is chosen by randomly selecting num_samples pairs of labeled nodes.
    For nodes, the agreement labels are 1.0 if the two nodes in a pair have the
    same label, or 0.0 otherwise.

    Arguments:
      labeled_samples: An array of integers representing the indices of the
        labeled nodes.
      num_samples: An integer representing the desired number of validation
        samples.
      data: A dataset object used to provided the labels of the labeled samples.
      ratio_pos_to_neg: A float repesenting the ratio of positive to negative
        samples.

    Returns:
      neighbors: An array of shape (num_samples, 2), where each row represents
        a pair of indices chosed from labeled_samples.
      agreement: An array of floats whose elements are either 1.0 or 0.0,
        representing the agreement value, as explained above.
    """
    neighbors = np.empty(shape=(num_samples, 2), dtype=np.int32)
    agreement = np.empty(shape=(num_samples,), dtype=np.float32)
    num_added = 0
    while num_added < num_samples:
      pair = self.rng.choice(labeled_samples, 2)
      pair_agrees = data.get_labels(pair[0]) == data.get_labels(pair[1])
      if ratio_pos_to_neg:
        # Keep positives and negatives balanced by rejection sampling.
        if ratio_pos_to_neg < 1 and not pair_agrees:
          random_number = self.rng.rand(1)[0]
          if random_number > ratio_pos_to_neg:
            continue
        elif ratio_pos_to_neg > 1 and pair_agrees:
          random_number = self.rng.rand(1)[0]
          if random_number > 1.0 / ratio_pos_to_neg:
            continue
      neighbors[num_added][0] = pair[0]
      neighbors[num_added][1] = pair[1]
      agreement[num_added] = pair_agrees
      num_added += 1
    return neighbors, agreement

  def _compute_ratio_pos_neg(self, labels):
    """Compute the agreement positive to negative sample ratio.

    Arguments:
      labels: An array containing labels for the labeled samples. Note that
        these are the labels for the classification task, not for the agreement
        prediction task, so they are in range [0, num_classes - 1].
    Returns:
      A float representing the ratio of positive / negative agreement labels.
    """
    # Compute how many of each label we have.
    label_counts = collections.Counter(labels)
    label_counts = np.asarray([count for count in label_counts.values()])
    # Convert the counts to ratios.
    label_counts = label_counts / np.sum(label_counts).astype(np.float32)
    # Use the ratios to compute the probability that a randomly sampled pair
    # of samples will have the same label.
    ratio = np.sum([r * r for r in label_counts])
    return ratio

  def train(self, data, session=None, **kwargs):
    """Train an agreement model."""

    summary_writer = kwargs['summary_writer']
    logging.info('Training agreement model...')

    if not self.is_initialized:
      self.is_initialized = True
    else:
      if self.weight_decay_update is not None:
        session.run(self.weight_decay_update)
        logging.info('New weight decay value:  %f',
                     session.run(self.weight_decay_var))

    # Construct data iterator.
    labeled_samples = data.get_indices_train()
    num_labeled_samples = len(labeled_samples)
    num_samples_train = num_labeled_samples * num_labeled_samples
    num_samples_val = min(int(num_samples_train * self.percent_val),
                          self.max_num_samples_val)

    if num_samples_train == 0:
      logging.info('No samples to train agreement. Skipping...')
      return None

    if not self.warm_start:
      # Re-initialize variables.
      initializers = [v.initializer for v in self.trainable_vars]
      initializers.append(self.global_step.initializer)
      session.run(initializers)
      # Reset the optimizer state (e.g., momentum).
      session.run(self.reset_optimizer)

    logging.info(
        'Training agreement with %d samples and validation on %d samples.',
        num_samples_train, num_samples_val)

    # Compute ratio of positives to negative samples.
    labeled_samples_labels = data.get_labels(labeled_samples)
    ratio_pos_to_neg = self._compute_ratio_pos_neg(labeled_samples_labels)
    # Select a validation set out of all pairs of labeled samples.
    neighbors_val, agreement_labels_val = self._select_val_set(
        labeled_samples, num_samples_val, data, ratio_pos_to_neg)
    # Create a train iterator that potentially excludes the validation samples.
    data_iterator_train = self._train_iterator(
        labeled_samples, neighbors_val, data, ratio_pos_to_neg=ratio_pos_to_neg)
    # Start training.
    best_val_acc = -1
    checkpoint_saved = False
    step = 0
    iter_below_tol = 0
    min_num_iter = self.min_num_iter
    has_converged = step >= self.max_num_iter
    if not has_converged:
      self.num_iter_trained += 1
    prev_loss_val = np.inf
    while not has_converged:
      feed_dict = self._construct_feed_dict(data_iterator_train, is_train=True)

      if self.enable_summaries and step % self.summary_step == 0:
        loss_val, summary, _ = session.run(
            [self.loss_op, self.summary_op, self.train_op],
            feed_dict=feed_dict)
        iter_total = session.run(self.iter_agr_total)
        summary_writer.add_summary(summary, iter_total)
        summary_writer.flush()
      else:
        loss_val, _ = session.run((self.loss_op, self.train_op),
                                  feed_dict=feed_dict)

      # Log the loss, if necessary.
      if step % self.logging_step == 0:
        logging.info('Agreement step %6d | Loss: %10.4f', step, loss_val)

      # Run validation, if necessary.
      if step % self.eval_step == 0:
        if num_samples_val == 0:
          logging.info('Skipping validation. No validation samples available.')
          break
        data_iterator_val = batch_iterator(
            neighbors_val,
            agreement_labels_val,
            self.batch_size,
            shuffle=False,
            allow_smaller_batch=True,
            repeat=False)
        feed_dict_val = self._construct_feed_dict(
            data_iterator_val, is_train=False)
        cummulative_val_acc = 0.0
        while feed_dict_val is not None:
          val_acc, batch_size_actual = session.run(
              (self.accuracy, self.batch_size_actual), feed_dict=feed_dict_val)
          cummulative_val_acc += val_acc * batch_size_actual
          feed_dict_val = self._construct_feed_dict(
              data_iterator_val, is_train=False)
        cummulative_val_acc /= num_samples_val

        # Evaluate over a random choice of sample pairs, either labeled or not.
        acc_random = self._eval_random_pairs(data, session)

        # Evaluate the accuracy on the latest train batch. We track this to make
        # sure the agreement model is able to fit the training data, but can be
        # eliminated if efficiency is an issue.
        acc_train = self._eval_train(session, feed_dict)

        if self.enable_summaries:
          summary = tf.Summary()
          summary.value.add(tag='AgreementModel/train_acc',
                            simple_value=acc_train)
          summary.value.add(tag='AgreementModel/val_acc',
                            simple_value=cummulative_val_acc)
          if acc_random is not None:
            summary.value.add(tag='AgreementModel/random_acc',
                              simple_value=acc_random)
          iter_total = session.run(self.iter_agr_total)
          summary_writer.add_summary(summary, iter_total)
          summary_writer.flush()
        if step % self.logging_step == 0 or cummulative_val_acc > best_val_acc:
          logging.info(
              'Agreement step %6d | Loss: %10.4f | val_acc: %10.4f |'
              'random_acc: %10.4f | acc_train: %10.4f', step, loss_val,
              cummulative_val_acc, acc_random, acc_train)
        if cummulative_val_acc > best_val_acc:
          best_val_acc = cummulative_val_acc
          if self.checkpoint_path:
            self.saver.save(
                session, self.checkpoint_path, write_meta_graph=False)
            checkpoint_saved = True
          # If we reached 100% accuracy, stop.
          if best_val_acc >= 1.00:
            logging.info('Reached 100% accuracy. Stopping...')
            break
          # Go for at least num_iter_after_best_val more iterations.
          min_num_iter = max(self.min_num_iter,
                             step + self.num_iter_after_best_val)
          logging.info(
              'Achieved best validation. '
              'Extending to at least %d iterations...', min_num_iter)

      step += 1
      has_converged, iter_below_tol = self.check_convergence(
          prev_loss_val,
          loss_val,
          step,
          self.max_num_iter,
          iter_below_tol,
          min_num_iter=min_num_iter)
      session.run(self.iter_agr_total_update)
      prev_loss_val = loss_val

    # Return to the best model.
    if checkpoint_saved:
      logging.info('Restoring best model...')
      self.saver.restore(session, self.checkpoint_path)

    return best_val_acc

  def predict(self, session, src_features, tgt_features, src_indices,
              tgt_indices):
    """Predict agreement for the provided pairs of samples.

    Note that here we don't need to use the src_indices and tgt_indices, but
    we keep them as inputs to this function because we want to have a common
    interface with the TrainerPerfectAgreement class.

    Arguments:
      session: A TensorFlow session where to run the model.
      src_features: An array of shape (num_samples, num_features) containing the
        features of the first element of the pair.
      tgt_features: An array of shape (num_samples, num_features) containing the
        features of the second element of the pair.
      src_indices: An array of integers containing the index of each sample in
        self.data of the samples in src_features.
      tgt_indices: An array of integers containing the index of each sample in
        self.data of the samples in tgt_features.

    Returns:
      An array containing the predicted agreement value for each pair of
      provided samples.
    """
    if self.num_iter_trained >= self.num_warm_up_iter:
      feed_dict = {
          self.src_features: src_features,
          self.tgt_features: tgt_features,
      }
      predictions = session.run(
          self.normalized_predictions, feed_dict=feed_dict)
      return predictions
    if self.agree_by_default:
      # Predict always agreement.
      return np.ones(shape=(len(src_features),), dtype=np.float32)
    # Predict always disagreement.
    return np.zeros(shape=(len(src_features),), dtype=np.float32)

  def _eval_train(self, session, feed_dict):
    """Computes the accuracy of the predictions for the provided batch.

    This calculates the accuracy for both class 1 (agreement) and class 0
    (disagreement).

    Arguments:
      session: A TensorFlow session.
      feed_dict: A train feed dictionary.
    Returns:
      The computed train accuracy.
    """
    train_acc, pred, targ = session.run(
        (self.accuracy, self.normalized_predictions, self.labels),
        feed_dict=feed_dict)
    # Assume the threshold is at 0.5, and binarize the predictions.
    binary_pred = pred > 0.5
    targ = targ.astype(np.int32)
    acc_per_sample = binary_pred == targ
    acc_1 = acc_per_sample[targ == 1]
    if acc_1.shape[0] > 0:
      acc_1 = sum(acc_1) / np.float32(len(acc_1))
    else:
      acc_1 = -1
    acc_0 = acc_per_sample[targ == 0]
    if acc_0.shape[0] > 0:
      acc_0 = sum(acc_0) / np.float32(len(acc_0))
    else:
      acc_0 = -1
    logging.info('Train acc: %.2f. Acc class 1: %.2f. Acc class 0: %.2f',
                 train_acc, acc_1, acc_0)
    return train_acc

  def predict_label_by_agreement(self, session, indices, num_neighbors=100):
    """Predict class labels using agreement with other labeled samples.

    Uses the agreement model to compute the agreement of a test sample with a
    subset of the labeled samples. Then it calculates the label distribution
    as a weighted average of the labeled samples, using the predicted agreement
    scores as weights.

    Arguments:
      session: A TensorFlow seession.
      indices: A list of integers representing the indices of the test samples
        to label.
      num_neighbors: An integer representing the number of labeled samples to
        compare each test sample with. The higher this number, the more accurate
        the predictions, but also the more expensive.
    Returns:
      acc: The accuracy of this agreement based classifier on the provided
        sample indices.
    """
    # Limit the number of labeled samples to compare with, for efficiency
    # reasons. At the moment we pick a random subset of labeled samples, but
    # perhaps there better ways (e.g. the closest samples in embedding space).
    train_indices = self.data.get_indices_train()
    num_train = train_indices.shape[0]
    if num_train > num_neighbors:
      selected = self.rng.choice(num_train, num_neighbors, replace=False)
      train_indices = train_indices[selected]
    num_labeled = train_indices.shape[0]
    train_labels = self.data.get_labels(train_indices)
    train_labels_1hot = np.zeros((num_labeled, self.data.num_classes))
    train_labels_1hot[np.arange(num_labeled), train_labels] = 1
    # For each sample for which we want to make predictions, we compute the
    # agreement with all selected labeled samples.
    agreement = np.zeros((num_labeled, 1))
    acc = 0.0
    for index_u in indices:
      # Pair the unlabeled sample with multiple labeled samples, and predict the
      # agreement in batches.
      features_u = self.data.get_features(index_u)
      features_u_batch = features_u[None].repeat(self.batch_size, axis=0)
      index_u_batch = np.repeat(index_u, self.batch_size)
      idx_start = 0
      while idx_start < num_labeled:
        # Select a batch of labeled samples.
        idx_end = idx_start + self.batch_size
        if idx_end > num_labeled:
          idx_end = num_labeled
          features_u_repeated = features_u[None].repeat(
              idx_end - idx_start, axis=0)
          index_u_repeated = np.repeat(index_u, idx_end - idx_start)
        else:
          features_u_repeated = features_u_batch
          index_u_repeated = index_u_batch
        batch_indices_l = train_indices[idx_start:idx_end]
        features_l = self.data.get_features(batch_indices_l)
        batch_agreement = self.predict(
            session=session,
            src_features=features_l,
            tgt_features=features_u_repeated,
            src_indices=batch_indices_l,
            tgt_indices=index_u_repeated)
        agreement[idx_start: idx_end, 0] = batch_agreement
        idx_start = idx_end
      # Cummulate the agreement weights per label.
      vote_per_label = np.sum(train_labels_1hot * agreement, axis=0)
      is_correct = (np.argmax(vote_per_label) ==
                    self.data.get_original_labels(index_u))
      acc += is_correct
    if indices:
      acc /= len(indices)
    logging.info('Majority vote accuracy: %.2f.', acc)
    return acc


class TrainerPerfectAgreement(object):
  """Trainer for an agreement model that always predicts the correct value."""

  def __init__(self, data):
    self.data = data
    self.model = None

    # Save the true labels in a TensorFlow variable, which is used in the
    # create_agreement_prediction function.
    with tf.variable_scope('perfect_agreement'):
      indices = np.arange(data.num_samples)
      self.labels = tf.get_variable(
          'labels_original',
          initializer=self.data.get_original_labels(indices))
      self.original_var_scope = tf.get_variable_scope()

  def train(self, unused_data, unused_session=None, **unused_kwargs):
    logging.info('Perfect agreement, no need to train...')

  def predict(self, unused_session, unused_src_features, unused_tgt_features,
              src_indices, tgt_indices):
    """Predict agreement for the provided pairs of samples.

    The predictions are perfect according to the original dataset ground truth
    labels.
    The function contains many unused arguments, in order to conform with the
    interface of the TrainerAgreement class.

    Arguments:
      unused_session: A TensorFlow session where to run the model.
      unused_src_features: An array of shape (num_samples, num_features)
        containing the features of the first element of the pair.
      unused_tgt_features: An array of shape (num_samples, num_features)
        containing the features of the second element of the pair.
      src_indices: An array of integers containing the index of each sample in
        self.data of the samples in src_features.
      tgt_indices: An array of integers containing the index of each sample in
        self.data of the samples in tgt_features.

    Returns:
      An array containing the predicted agreement value for each pair of
      provided samples.
    """
    agreement = [
        self.data.get_original_labels(s) == self.data.get_original_labels(t)
        for s, t in zip(src_indices, tgt_indices)]
    return np.asarray(agreement, dtype=np.float32)

  def create_agreement_prediction(self, src_indices, tgt_indices,
                                  **unused_kwargs):
    """Creates the agreement prediction TensorFlow subgraph.

    This function is the equivalent of `create_agreement_prediction` in
    TrainerAgreement, but here we use the oracle labels to make the agreement
    prediction.

    Arguments:
      src_indices: A Tensor or Placeholder of shape (batch_size,)
        containing the indices of the samples that are the sources of the edges.
      tgt_indices: A Tensor or Placeholder of shape (batch_size,)
        containing the indices of the samples that are the targets of the edges.
      unused_kwargs: Other unused keyword arguments, which we allow in order to
        create a common interface with TrainerAgreement.
    Returns:
      predictions: None, because this model doesn't do logits computations, but
        we still return something in order to keep the same function outputs as
        TrainerAgreement.
      normalized_predictions: A Tensor of shape (batch_size,) with values in
        {0, 1}, containing the agreement prediction probabilities.
      variables: An empty dictionary of trainable variables, because this model
        does not have any trainable variables.
      reg_params: An empty dictionary of variables that are used in the
        regularization weight decay term, because this model doesn't have
        regularization variables.
    """
    with tf.variable_scope(self.original_var_scope, auxiliary_name_scope=False,
                           reuse=True):
      src_labels = tf.gather(self.labels, src_indices)
      tgt_labels = tf.gather(self.labels, tgt_indices)
      agreement = tf.equal(src_labels, tgt_labels)
    return None, tf.cast(agreement, tf.float32), {}, {}

  def predict_label_by_agreement(self, indices, num_neighbors=100,
                                 **unused_kwargs):
    """Predict class labels using agreement with other labeled samples.

    Uses the agreement model to compute the agreement of a test sample with a
    subset of the labeled samples. Then it calculates the label distribution
    as a weighted average of the labeled samples, using the predicted agreement
    scores as weights.

    Arguments:
      indices: A list of integers representing the indices of the test samples
        to label.
      num_neighbors: An integer representing the number of labeled samples to
        compare each test sample with. The higher this number, the more accurate
        the predictions, but also the more expensive.
      **unused_kwargs: Other keyword arguments that may be provided just to
        have a simiar interface as TrainerAgreement, when calling
        predict_label_by_agreement from the classification model.
    Returns:
      acc: The accuracy of this agreement based classifier on the provided
        sample indices.
    """
    # Limit the number of labeled samples to compare with, for efficiency
    # reasons. At the moment we pick a random subset of labeled samples, but
    # perhaps there better ways (e.g. the closest samples in embedding space).
    train_indices = np.asarray(list(self.data.get_indices_train()))
    if len(train_indices) > num_neighbors:
      self.rng.shuffle(train_indices)
      train_indices = train_indices[:num_neighbors]
    num_labeled = train_indices.shape[0]
    train_labels = self.data.get_labels(train_indices)
    train_labels_1hot = np.zeros((num_labeled, self.data.num_classes))
    train_labels_1hot[np.arange(num_labeled), train_labels] = 1
    train_labels_original = self.data.get_original_labels(train_indices)
    # For each sample for which we want to make predictions, we compute the
    # agreement with all selected labeled samples.
    acc = 0.0
    for index_u in indices:
      label_u = self.data.get_original_labels(index_u)
      agreement = train_labels_original == label_u
      # Cummulate the agreement weights per label.
      vote_per_label = np.sum(train_labels_1hot[agreement], axis=0)
      is_correct = (np.argmax(vote_per_label) ==
                    self.data.get_original_labels(index_u))
      acc += is_correct
    if indices:
      acc /= len(indices)
    logging.info('Majority vote accuracy: %.2f.', acc)
    return acc
