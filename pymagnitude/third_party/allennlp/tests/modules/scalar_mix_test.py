# pylint: disable=no-self-use,invalid-name



from __future__ import with_statement
from __future__ import division
from __future__ import absolute_import
import torch

import pytest
import numpy

from allennlp.common.testing import AllenNlpTestCase
from allennlp.common.checks import ConfigurationError
from allennlp.modules import ScalarMix


class TestScalarMix(AllenNlpTestCase):
    def test_scalar_mix_can_run_forward(self):
        mixture = ScalarMix(3)
        tensors = [torch.randn([3, 4, 5]) for _ in range(3)]
        for k in range(3):
            mixture.scalar_parameters[k].data[0] = 0.1 * (k + 1)
        mixture.gamma.data[0] = 0.5
        result = mixture(tensors)

        weights = [0.1, 0.2, 0.3]
        normed_weights = numpy.exp(weights) / numpy.sum(numpy.exp(weights))
        expected_result = sum(normed_weights[k] * tensors[k].data.numpy() for k in range(3))
        expected_result *= 0.5
        numpy.testing.assert_almost_equal(expected_result, result.data.numpy())

    def test_scalar_mix_throws_error_on_incorrect_number_of_inputs(self):
        mixture = ScalarMix(3)
        tensors = [torch.randn([3, 4, 5]) for _ in range(5)]
        with pytest.raises(ConfigurationError):
            _ = mixture(tensors)

    def test_scalar_mix_layer_norm(self):
        mixture = ScalarMix(3, do_layer_norm=u'scalar_norm_reg')

        tensors = [torch.randn([3, 4, 5]) for _ in range(3)]
        numpy_mask = numpy.ones((3, 4), dtype=u'int32')
        numpy_mask[1, 2:] = 0
        mask = torch.from_numpy(numpy_mask)

        weights = [0.1, 0.2, 0.3]
        for k in range(3):
            mixture.scalar_parameters[k].data[0] = weights[k]
        mixture.gamma.data[0] = 0.5
        result = mixture(tensors, mask)

        normed_weights = numpy.exp(weights) / numpy.sum(numpy.exp(weights))
        expected_result = numpy.zeros((3, 4, 5))
        for k in range(3):
            mean = numpy.mean(tensors[k].data.numpy()[numpy_mask == 1])
            std = numpy.std(tensors[k].data.numpy()[numpy_mask == 1])
            normed_tensor = (tensors[k].data.numpy() - mean) / (std + 1E-12)
            expected_result += (normed_tensor * normed_weights[k])
        expected_result *= 0.5

        numpy.testing.assert_almost_equal(expected_result, result.data.numpy())
