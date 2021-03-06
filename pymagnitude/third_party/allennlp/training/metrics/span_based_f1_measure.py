

from __future__ import division
from __future__ import absolute_import
#typing
from collections import defaultdict

import torch

from allennlp.common.checks import ConfigurationError
from allennlp.nn.util import get_lengths_from_binary_sequence_mask
from allennlp.data.vocabulary import Vocabulary
from allennlp.training.metrics.metric import Metric
from allennlp.data.dataset_readers.dataset_utils.span_utils import (
        bio_tags_to_spans,
        bioul_tags_to_spans,
        iob1_tags_to_spans,
        TypedStringSpan
)


class SpanBasedF1Measure(Metric):
    u"""
    The Conll SRL metrics are based on exact span matching. This metric
    implements span-based precision and recall metrics for a BIO tagging
    scheme. It will produce precision, recall and F1 measures per tag, as
    well as overall statistics. Note that the implementation of this metric
    is not exactly the same as the perl script used to evaluate the CONLL 2005
    data - particularly, it does not consider continuations or reference spans
    as constituents of the original span. However, it is a close proxy, which
    can be helpful for judging model peformance during training. This metric
    works properly when the spans are unlabeled (i.e., your labels are
    simply "B", "I", "O" if using the "BIO" label encoding).

    """
    def __init__(self,
                 vocabulary            ,
                 tag_namespace      = u"tags",
                 ignore_classes            = None,
                 label_encoding      = u"BIO")        :
        u"""
        Parameters
        ----------
        vocabulary : ``Vocabulary``, required.
            A vocabulary containing the tag namespace.
        tag_namespace : str, required.
            This metric assumes that a BIO format is used in which the
            labels are of the format: ["B-LABEL", "I-LABEL"].
        ignore_classes : List[str], optional.
            Span labels which will be ignored when computing span metrics.
            A "span label" is the part that comes after the BIO label, so it
            would be "ARG1" for the tag "B-ARG1". For example by passing:

             ``ignore_classes=["V"]``
            the following sequence would not consider the "V" span at index (2, 3)
            when computing the precision, recall and F1 metrics.

            ["O", "O", "B-V", "I-V", "B-ARG1", "I-ARG1"]

            This is helpful for instance, to avoid computing metrics for "V"
            spans in a BIO tagging scheme which are typically not included.
        label_encoding : ``str``, optional (default = "BIO")
            The encoding used to specify label span endpoints in the sequence.
            Valid options are "BIO", "IOB1", or BIOUL".
        """
        if label_encoding not in [u"BIO", u"IOB1", u"BIOUL"]:
            raise ConfigurationError(u"Unknown label encoding - expected 'BIO', 'IOB1', 'BIOUL'.")

        self._label_encoding = label_encoding
        self._label_vocabulary = vocabulary.get_index_to_token_vocabulary(tag_namespace)
        self._ignore_classes =  ignore_classes or []

        # These will hold per label span counts.
        self._true_positives =  defaultdict(int)
        self._false_positives =  defaultdict(int)
        self._false_negatives =  defaultdict(int)

    def __call__(self,
                 predictions              ,
                 gold_labels              ,
                 mask                         = None,
                 prediction_map                         = None):
        u"""
        Parameters
        ----------
        predictions : ``torch.Tensor``, required.
            A tensor of predictions of shape (batch_size, sequence_length, num_classes).
        gold_labels : ``torch.Tensor``, required.
            A tensor of integer class label of shape (batch_size, sequence_length). It must be the same
            shape as the ``predictions`` tensor without the ``num_classes`` dimension.
        mask: ``torch.Tensor``, optional (default = None).
            A masking tensor the same size as ``gold_labels``.
        prediction_map: ``torch.Tensor``, optional (default = None).
            A tensor of size (batch_size, num_classes) which provides a mapping from the index of predictions
            to the indices of the label vocabulary. If provided, the output label at each timestep will be
            ``vocabulary.get_index_to_token_vocabulary(prediction_map[batch, argmax(predictions[batch, t]))``,
            rather than simply ``vocabulary.get_index_to_token_vocabulary(argmax(predictions[batch, t]))``.
            This is useful in cases where each Instance in the dataset is associated with a different possible
            subset of labels from a large label-space (IE FrameNet, where each frame has a different set of
            possible roles associated with it).
        """
        if mask is None:
            mask = torch.ones_like(gold_labels)

        predictions, gold_labels, mask, prediction_map = self.unwrap_to_tensors(predictions,
                                                                                gold_labels,
                                                                                mask, prediction_map)

        num_classes = predictions.size(-1)
        if (gold_labels >= num_classes).any():
            raise ConfigurationError(u"A gold label passed to SpanBasedF1Measure contains an "
                                     u"id >= {}, the number of classes.".format(num_classes))

        sequence_lengths = get_lengths_from_binary_sequence_mask(mask)
        argmax_predictions = predictions.max(-1)[1]

        if prediction_map is not None:
            argmax_predictions = torch.gather(prediction_map, 1, argmax_predictions)
            gold_labels = torch.gather(prediction_map, 1, gold_labels.long())

        argmax_predictions = argmax_predictions.float()

        # Iterate over timesteps in batch.
        batch_size = gold_labels.size(0)
        for i in range(batch_size):
            sequence_prediction = argmax_predictions[i, :]
            sequence_gold_label = gold_labels[i, :]
            length = sequence_lengths[i]

            if length == 0:
                # It is possible to call this metric with sequences which are
                # completely padded. These contribute nothing, so we skip these rows.
                continue

            predicted_string_labels = [self._label_vocabulary[label_id]
                                       for label_id in sequence_prediction[:length].tolist()]
            gold_string_labels = [self._label_vocabulary[label_id]
                                  for label_id in sequence_gold_label[:length].tolist()]

            if self._label_encoding == u"BIO":
                predicted_spans = bio_tags_to_spans(predicted_string_labels, self._ignore_classes)
                gold_spans = bio_tags_to_spans(gold_string_labels, self._ignore_classes)
            elif self._label_encoding == u"IOB1":
                predicted_spans = iob1_tags_to_spans(predicted_string_labels, self._ignore_classes)
                gold_spans = iob1_tags_to_spans(gold_string_labels, self._ignore_classes)
            elif self._label_encoding == u"BIOUL":
                predicted_spans = bioul_tags_to_spans(predicted_string_labels, self._ignore_classes)
                gold_spans = bioul_tags_to_spans(gold_string_labels, self._ignore_classes)

            predicted_spans = self._handle_continued_spans(predicted_spans)
            gold_spans = self._handle_continued_spans(gold_spans)

            for span in predicted_spans:
                if span in gold_spans:
                    self._true_positives[span[0]] += 1
                    gold_spans.remove(span)
                else:
                    self._false_positives[span[0]] += 1
            # These spans weren't predicted.
            for span in gold_spans:
                self._false_negatives[span[0]] += 1

    @staticmethod
    def _handle_continued_spans(spans                       )                         :
        u"""
        The official CONLL 2012 evaluation script for SRL treats continued spans (i.e spans which
        have a `C-` prepended to another valid tag) as part of the span that they are continuing.
        This is basically a massive hack to allow SRL models which produce a linear sequence of
        predictions to do something close to structured prediction. However, this means that to
        compute the metric, these continutation spans need to be merged into the span to which
        they refer. The way this is done is to simply consider the span for the continued argument
        to start at the start index of the first occurence of the span and end at the end index
        of the last occurence of the span. Handling this is important, because predicting continued
        spans is difficult and typically will effect overall average F1 score by ~ 2 points.

        Parameters
        ----------
        spans : ``List[TypedStringSpan]``, required.
            A list of (label, (start, end)) spans.

        Returns
        -------
        A ``List[TypedStringSpan]`` with continued arguments replaced with a single span.
        """
        span_set                       = set(spans)
        continued_labels            = [label[2:] for (label, span) in span_set if label.startswith(u"C-")]
        for label in continued_labels:
            continued_spans = set(span for span in span_set if label in span[0])

            span_start = min(span[1][0] for span in continued_spans)
            span_end = max(span[1][1] for span in continued_spans)
            replacement_span                  = (label, (span_start, span_end))

            span_set.difference_update(continued_spans)
            span_set.add(replacement_span)

        return list(span_set)

    def get_metric(self, reset       = False):
        u"""
        Returns
        -------
        A Dict per label containing following the span based metrics:
        precision : float
        recall : float
        f1-measure : float

        Additionally, an ``overall`` key is included, which provides the precision,
        recall and f1-measure for all spans.
        """
        all_tags           = set()
        all_tags.update(list(self._true_positives.keys()))
        all_tags.update(list(self._false_positives.keys()))
        all_tags.update(list(self._false_negatives.keys()))
        all_metrics = {}
        for tag in all_tags:
            precision, recall, f1_measure = self._compute_metrics(self._true_positives[tag],
                                                                  self._false_positives[tag],
                                                                  self._false_negatives[tag])
            precision_key = u"precision" + u"-" + tag
            recall_key = u"recall" + u"-" + tag
            f1_key = u"f1-measure" + u"-" + tag
            all_metrics[precision_key] = precision
            all_metrics[recall_key] = recall
            all_metrics[f1_key] = f1_measure

        # Compute the precision, recall and f1 for all spans jointly.
        precision, recall, f1_measure = self._compute_metrics(sum(self._true_positives.values()),
                                                              sum(self._false_positives.values()),
                                                              sum(self._false_negatives.values()))
        all_metrics[u"precision-overall"] = precision
        all_metrics[u"recall-overall"] = recall
        all_metrics[u"f1-measure-overall"] = f1_measure
        if reset:
            self.reset()
        return all_metrics

    @staticmethod
    def _compute_metrics(true_positives     , false_positives     , false_negatives     ):
        precision = float(true_positives) / float(true_positives + false_positives + 1e-13)
        recall = float(true_positives) / float(true_positives + false_negatives + 1e-13)
        f1_measure = 2. * ((precision * recall) / (precision + recall + 1e-13))
        return precision, recall, f1_measure

    def reset(self):
        self._true_positives = defaultdict(int)
        self._false_positives = defaultdict(int)
        self._false_negatives = defaultdict(int)

SpanBasedF1Measure = Metric.register(u"span_f1")(SpanBasedF1Measure)
