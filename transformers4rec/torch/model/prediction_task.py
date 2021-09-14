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
#


import copy
import logging
from types import SimpleNamespace
from typing import Callable, Dict, Iterable, Optional, Union

import torch
import torchmetrics as tm
from transformers.modeling_utils import SequenceSummary

from merlin_standard_lib.registry import camelcase_to_snakecase

from ..block.base import Block, BuildableBlock, SequentialBlock
from ..block.mlp import MLPBlock
from ..ranking_metric import AvgPrecisionAt, NDCGAt, RecallAt
from ..typing import BlockType, Head, InputBlock, Model, TabularData
from ..utils.torch_utils import LambdaModule, LossMixin, MetricsMixin

LOG = logging.getLogger("transformers4rec")


def name_fn(name, inp):
    return "/".join([name, inp]) if name else None


class PredictionTask(torch.nn.Module, LossMixin, MetricsMixin):
    """Individual prediction-task of a model.

    Parameters
    ----------
    loss: torch.nn.Module
        The loss to use during training of this task.
    metrics: torch.nn.Module
        The metrics to calculate during training & evaluation.
    target_name: str, optional
        Name of the target, this is needed when there are multiple targets.
    task_name: str, optional
        Name of the prediction task, if not provided a name will be automatically constructed based
        on the target-name & class-name.
    forward_to_prediction_fn: Callable[[torch.Tensor], torch.Tensor]
        Function to apply before the prediction
    task_block: BlockType
        Module to transform input tensor before computing predictions.
    pre: BlockType
        Module to compute the predictions probabilities.
    summary_type: str
        This is used to summarize a sequence into a single tensor. Accepted values are:
            - `"last"` -- Take the last token hidden state (like XLNet)
            - `"first"` -- Take the first token hidden state (like Bert)
            - `"mean"` -- Take the mean of all tokens hidden states
            - `"cls_index"` -- Supply a Tensor of classification token position (GPT/GPT-2)
            - `"attn"` -- Not implemented now, use multi-head attention
    """

    def __init__(
        self,
        loss: torch.nn.Module,
        metrics: Iterable[tm.Metric] = None,
        target_name: Optional[str] = None,
        task_name: Optional[str] = None,
        forward_to_prediction_fn: Callable[[torch.Tensor], torch.Tensor] = lambda x: x,
        task_block: Optional[BlockType] = None,
        pre: Optional[BlockType] = None,
        summary_type: str = "last",
    ):
        super().__init__()
        self.sequence_summary = SequenceSummary(SimpleNamespace(summary_type=summary_type))  # noqa
        self.target_name = target_name
        self.forward_to_prediction_fn = forward_to_prediction_fn
        self.set_metrics(metrics)
        self.loss = loss
        self.pre = pre
        self.task_block = task_block
        self._task_name = task_name

    def build(
        self,
        body: BlockType,
        input_size,
        inputs: Optional[InputBlock] = None,
        device=None,
        task_block=None,
        pre=None,
    ):
        """
        The method will be called when block is converted to a model,
        i.e when linked to prediction head.

        Parameters
        ----------
        block:
            the model block to link with head
        device:
            set the device for the metrics and layers of the task
        """
        if task_block:
            # TODO: What to do when `self.task_block is not None`?
            self.task_block = task_block
        if pre:
            # TODO: What to do when `self.pre is not None`?
            self.pre = pre

        # Build task block
        pre_input_size = input_size
        if self.task_block:
            if isinstance(self.task_block, torch.nn.Module):
                self.task_block = copy.deepcopy(self.task_block)
            else:
                self.task_block = self.task_block.build(input_size)
            pre_input_size = self.task_block.output_size()

        if self.pre:
            if isinstance(self.pre, torch.nn.Module):
                self.pre = copy.deepcopy(self.pre)
            else:
                self.pre = self.pre.build(pre_input_size)

        if device:
            self.to(device)
            for metric in self.metrics:
                metric.to(device)
        self.built = True

    def forward(self, inputs, **kwargs):
        x = inputs

        if len(x.size()) == 3:
            x = self.sequence_summary(x)

        if self.task_block:
            x = self.task_block(x)

        if self.pre:
            x = self.pre(x)

        return x

    @property
    def task_name(self):
        if self._task_name:
            return self._task_name

        base_name = camelcase_to_snakecase(self.__class__.__name__)

        return name_fn(self.target_name, base_name) if self.target_name else base_name

    def child_name(self, name):
        return name_fn(self.task_name, name)

    def set_metrics(self, metrics):
        self.metrics = torch.nn.ModuleList(metrics)

    def compute_loss(
        self,
        inputs: Union[torch.Tensor, TabularData],
        targets: Union[torch.Tensor, TabularData],
        compute_metrics: bool = True,
        training: bool = False,
        **kwargs,
    ) -> torch.Tensor:
        if isinstance(targets, dict) and self.target_name:
            targets = targets[self.target_name]

        predictions = self(inputs, training=training)
        loss = self.loss(predictions, targets)

        if compute_metrics:
            self.calculate_metrics(predictions, targets, mode="train", forward=False)

            return loss

        return loss

    def calculate_metrics(
        self,
        predictions: Union[torch.Tensor, TabularData],
        targets: Union[torch.Tensor, TabularData],
        mode: str = "val",
        forward: bool = True,
        **kwargs,
    ) -> Dict[str, Union[Dict[str, torch.Tensor], torch.Tensor]]:
        if isinstance(targets, dict) and self.target_name:
            targets = targets[self.target_name]

        outputs = {}
        if forward:
            predictions = self(predictions)
        predictions = self.forward_to_prediction_fn(predictions)

        for metric in self.metrics:
            if isinstance(metric, tuple(type(x) for x in BinaryClassificationTask.DEFAULT_METRICS)):
                targets = targets.int()
            outputs[self.metric_name(metric)] = metric(predictions, targets)

        return outputs

    def compute_metrics(self, **kwargs):
        return {self.metric_name(metric): metric.compute() for metric in self.metrics}

    def metric_name(self, metric: tm.Metric) -> str:
        return self.child_name(camelcase_to_snakecase(metric.__class__.__name__))

    def reset_metrics(self):
        for metric in self.metrics:
            metric.reset()

    def to_head(self, body, inputs=None, **kwargs) -> "Head":
        from .head import Head as _Head

        return _Head(body, self, inputs=inputs, **kwargs)

    def to_model(self, body, inputs=None, **kwargs) -> Model:
        from .head import Head as _Head
        from .model import Model as _Model

        return _Model(_Head(body, self, inputs=inputs, **kwargs), **kwargs)


class BinaryClassificationPrepareBlock(BuildableBlock):
    def build(self, input_size) -> SequentialBlock:
        return SequentialBlock(
            torch.nn.Linear(input_size[-1], 1, bias=False),
            torch.nn.Sigmoid(),
            LambdaModule(lambda x: x.view(-1)),
            output_size=[
                None,
            ],
        )


class BinaryClassificationTask(PredictionTask):
    DEFAULT_LOSS = torch.nn.BCELoss()
    DEFAULT_METRICS = (
        tm.Precision(num_classes=2),
        tm.Recall(num_classes=2),
        tm.Accuracy(),
        # TODO: Fix this: tm.AUC()
    )

    def __init__(
        self,
        target_name: Optional[str] = None,
        task_name: Optional[str] = None,
        task_block: Optional[BlockType] = None,
        loss=DEFAULT_LOSS,
        metrics=DEFAULT_METRICS,
        summary_type="first",
    ):
        super().__init__(
            loss=loss,
            metrics=metrics,
            target_name=target_name,
            task_name=task_name,
            summary_type=summary_type,
            task_block=task_block,
            pre=BinaryClassificationPrepareBlock(),
            forward_to_prediction_fn=lambda x: torch.round(x).int(),
        )


class RegressionPrepareBlock(BuildableBlock):
    def build(self, input_size) -> SequentialBlock:
        return SequentialBlock(
            torch.nn.Linear(input_size[-1], 1),
            LambdaModule(lambda x: x.view(-1)),
            output_size=[
                None,
            ],
        )


class RegressionTask(PredictionTask):
    DEFAULT_LOSS = torch.nn.MSELoss()
    DEFAULT_METRICS = (tm.regression.MeanSquaredError(),)

    def __init__(
        self,
        target_name: Optional[str] = None,
        task_name: Optional[str] = None,
        task_block: Optional[BlockType] = None,
        loss=DEFAULT_LOSS,
        metrics=DEFAULT_METRICS,
        summary_type="first",
    ):
        super().__init__(
            loss=loss,
            metrics=metrics,
            target_name=target_name,
            task_name=task_name,
            summary_type=summary_type,
            task_block=task_block,
            pre=RegressionPrepareBlock(),
        )


class NextItemPredictionTask(PredictionTask):
    """Next-item prediction task.

    Parameters:
    ----------
    loss: torch.nn.Module
        Loss function to use. Defaults to NLLLos.
    metrics: Iterable[torchmetrics.Metric]
        List of ranking metrics to use for evaluation.
    task_block:
        Module to transform input tensor before computing predictions.
    task_name: str, optional
        Name of the prediction task, if not provided a name will be automatically constructed based
        on the target-name & class-name.
    weight_tying: bool
        The item id embedding table weights are shared with the prediction network layer.
    softmax_temperature: float
        Softmax temperature, used to reduce model overconfidence, so that softmax(logits / T).
        Value 1.0 reduces to regular softmax.
    padding_idx: int
        pad token id.
    target_dim: int
        vocabulary size of item ids
    hf_format: bool
        Output the dictionary of outputs needed by RecSysTrainer, if set to False,
        return the predictions tensor.
    """

    DEFAULT_METRICS = (
        # default metrics suppose labels are int encoded
        NDCGAt(top_ks=[10, 20], labels_onehot=True),
        AvgPrecisionAt(top_ks=[10, 20], labels_onehot=True),
        RecallAt(top_ks=[10, 20], labels_onehot=True),
    )

    def __init__(
        self,
        loss: torch.nn.Module = torch.nn.NLLLoss(ignore_index=0),
        metrics: Iterable[tm.Metric] = DEFAULT_METRICS,
        task_block: Optional[torch.nn.Module] = None,
        task_name: str = "next-item",
        weight_tying: bool = False,
        softmax_temperature: float = 1,
        padding_idx: int = 0,
        target_dim: int = None,
        hf_format=False,
    ):
        super().__init__(loss=loss, metrics=metrics, task_block=task_block, task_name=task_name)
        self.softmax_temperature = softmax_temperature
        self.weight_tying = weight_tying
        self.padding_idx = padding_idx
        self.target_dim = target_dim
        self.hf_format = hf_format

        self.item_embedding_table = None
        self.masking = None

    def build(self, body, input_size, device=None, inputs=None, task_block=None, pre=None):
        """Build method, this is called by the `Head`."""
        if not len(input_size) == 3 or isinstance(input_size, dict):
            raise ValueError(
                "NextItemPredictionTask needs a 3-dim vector as input, found:" f"{input_size}"
            )

        # Retrieve the embedding module to get the name of itemid col and its related table
        if not inputs:
            inputs = body.inputs
        if not getattr(inputs, "item_id", None):
            raise ValueError(
                "For Item Prediction task a categorical_module "
                "including an item_id column is required."
            )
        self.embeddings = inputs.categorical_module
        if not self.target_dim:
            self.target_dim = self.embeddings.item_embedding_table.num_embeddings
        if self.weight_tying:
            self.item_embedding_table = self.embeddings.item_embedding_table
            item_dim = self.item_embedding_table.weight.shape[1]
            if input_size[-1] != item_dim and not task_block:
                LOG.warning(
                    f"Projecting inputs of NextItemPredictionTask to'{item_dim}' "
                    f"As weight tying requires the input dimension '{input_size[-1]}' "
                    f"to be equal to the item-id embedding dimension '{item_dim}'"
                )
                # project input tensors to same dimension as item-id embeddings
                task_block = MLPBlock([item_dim])

        # Retrieve the masking if used in the model block
        self.masking = inputs.masking
        if self.masking:
            self.padding_idx = self.masking.padding_idx

        pre = NextItemPredictionPrepareBlock(
            target_dim=self.target_dim,
            weight_tying=self.weight_tying,
            item_embedding_table=self.item_embedding_table,
            softmax_temperature=self.softmax_temperature,
        )
        super().build(
            body, input_size, device=device, inputs=inputs, task_block=task_block, pre=pre
        )

    def forward(self, inputs: torch.Tensor, **kwargs):
        if isinstance(inputs, (tuple, list)):
            inputs = inputs[0]
        x = inputs.float()

        if self.task_block:
            x = self.task_block(x)

        # Retrieve labels either from masking or input module
        if self.masking:
            labels = self.masking.masked_targets
        else:
            labels = self.embeddings.item_seq

        # remove padded items
        trg_flat = labels.flatten()
        non_pad_mask = trg_flat != self.padding_idx
        labels_all = torch.masked_select(trg_flat, non_pad_mask)
        x = self.remove_pad_3d(x, non_pad_mask)

        # Compute predictions probs
        x = self.pre(x)

        # prepare outputs for HF trainer
        if self.hf_format:
            loss = self.loss(x, labels_all)
            return {
                "loss": loss,
                "labels": labels_all,
                "predictions": x,
                "pred_metadata": {},
                "model_outputs": [],
            }
            # TODO: Add model_outputs and metadata

        return x

    def remove_pad_3d(self, inp_tensor, non_pad_mask):
        # inp_tensor: (n_batch x seqlen x emb_dim)
        inp_tensor = inp_tensor.flatten(end_dim=1)
        inp_tensor_fl = torch.masked_select(
            inp_tensor, non_pad_mask.unsqueeze(1).expand_as(inp_tensor)
        )
        out_tensor = inp_tensor_fl.view(-1, inp_tensor.size(1))
        return out_tensor

    def calculate_metrics(
        self, predictions, targets, mode="val", forward=True, **kwargs
    ) -> Dict[str, torch.Tensor]:
        if isinstance(targets, dict) and self.target_name:
            targets = targets[self.target_name]

        outputs = {}
        if forward:
            predictions = self(predictions)
            if self.hf_format:
                targets = predictions["labels"]
                predictions = predictions["predictions"]
        predictions = self.forward_to_prediction_fn(predictions)

        for metric in self.metrics:
            outputs[self.metric_name(metric)] = metric(predictions, targets)

        return outputs

    def compute_metrics(self):
        metrics = {
            self.metric_name(metric): metric.compute()
            for metric in self.metrics
            if getattr(metric, "top_ks", None)
        }
        # Explode metrics for each cut-off
        # TODO make result generic:
        # To accept a mix of ranking metrics and others not requiring top_ks ?
        topks = {self.metric_name(metric): metric.top_ks for metric in self.metrics}
        results = {}
        for name, metric in metrics.items():
            for measure, k in zip(metric, topks[name]):
                results[f"{name}_{k}"] = measure
        return results


class NextItemPredictionPrepareBlock(BuildableBlock):
    def __init__(
        self,
        target_dim: int,
        weight_tying: bool = False,
        item_embedding_table: Optional[torch.nn.Module] = None,
        softmax_temperature: float = 0,
    ):
        super().__init__()
        self.target_dim = target_dim
        self.weight_tying = weight_tying
        self.item_embedding_table = item_embedding_table
        self.softmax_temperature = softmax_temperature

    def build(self, input_size) -> Block:
        return Block(
            _NextItemPredictionTask(
                input_size,
                self.target_dim,
                self.weight_tying,
                self.item_embedding_table,
                self.softmax_temperature,
            ),
            [None, self.target_dim],
        )


class _NextItemPredictionTask(torch.nn.Module):
    """Predict the interacted item-id probabilities.

    - During inference, the task consists of predicting the next item.
    - During training, the class supports the following Language modeling tasks:
        Causal LM, Masked LM, Permutation LM and Replacement Token Detection

    Parameters:
    -----------
        input_size: int
            Input size of this module.
        target_dim: int
            Dimension of the target.
        weight_tying: bool
            The item id embedding table weights are shared with the prediction network layer.
        item_embedding_table: torch.nn.Module
            Module that's used to store the embedding table for the item.
        softmax_temperature: float
            Softmax temperature, used to reduce model overconfidence, so that softmax(logits / T).
            Value 1.0 reduces to regular softmax.
    """

    def __init__(
        self,
        input_size: int,
        target_dim: int,
        weight_tying: bool = False,
        item_embedding_table: Optional[torch.nn.Module] = None,
        softmax_temperature: float = 0,
    ):
        super().__init__()
        self.input_size = input_size
        self.target_dim = target_dim
        self.weight_tying = weight_tying
        self.item_embedding_table = item_embedding_table
        self.softmax_temperature = softmax_temperature
        self.log_softmax = torch.nn.LogSoftmax(dim=-1)

        if self.weight_tying:
            self.output_layer_bias = torch.nn.Parameter(torch.Tensor(self.target_dim))
            torch.nn.init.zeros_(self.output_layer_bias)
        else:
            self.output_layer = torch.nn.Linear(self.input_size[-1], self.target_dim)

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        if self.weight_tying:
            logits = torch.nn.functional.linear(
                inputs,
                weight=self.item_embedding_table.weight,
                bias=self.output_layer_bias,
            )
        else:
            logits = self.output_layer(inputs)

        if self.softmax_temperature:
            # Softmax temperature to reduce model overconfidence
            # and better calibrate probs and accuracy
            logits = torch.div(logits, self.softmax_temperature)

        predictions = self.log_softmax(logits)

        return predictions

    def _get_name(self) -> str:
        return "NextItemPredictionTask"