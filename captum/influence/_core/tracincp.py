#!/usr/bin/env python3

import glob
import warnings
from abc import abstractmethod
from os.path import join
from typing import (
    Any,
    Callable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Tuple,
    Type,
    Union,
)

import torch
from captum._utils.av import AV
from captum._utils.common import _format_inputs
from captum._utils.gradient import (
    _compute_jacobian_wrt_params,
    _compute_jacobian_wrt_params_with_sample_wise_trick,
)
from captum._utils.progress import progress
from captum.influence._core.influence import DataInfluence
from captum.influence._utils.common import (
    _format_inputs_dataset,
    _get_k_most_influential_helper,
    _gradient_dot_product,
    _load_flexible_state_dict,
    _self_influence_by_batches_helper,
)
from captum.log import log_usage
from torch import Tensor
from torch.nn import Module
from torch.utils.data import DataLoader, Dataset


r"""

Note: methods starting with "_" are protected, not private, and can be overridden in
child classes.  They are not part of the API.

Implements abstract DataInfluence class and provides implementation details for
influence computation based on the logic provided in TracIn paper
(https://arxiv.org/abs/2002.08484).

The TracIn paper proposes an idealized notion of influence which can be represented by
the total amount a training example reduces loss for a test example via a training
process such as stochastic gradient descent. As this idealized notion of influence is
impractical to compute, the TracIn paper proposes instead to compute an influence
score, which uses a first-order approximation for the change in loss for a test example
by a training example, which is accumulated across saved model checkpoints. This
influence score is accumulated via a summed dot-product of gradient vectors for the
scores/loss of a test and training example.
"""

"""
TODO: Support for checkpoint type. Currently only supports model parameters as saved
checkpoints. Can use enum or string.

Potential implementation from design doc:
checkpoint_type (Enum = [Parameters | Loss_Grad]): For performance,
                saved / loaded checkpoints can be either model parameters, or
                gradient of the loss function on an input w.r.t parameters.
"""


class KMostInfluentialResults(NamedTuple):
    """
    This namedtuple stores the results of using the `influence` method. This method
    is implemented by all subclasses of `TracInCPBase` to calculate
    proponents / opponents. The `indices` field stores the indices of the
    proponents / opponents for each example in the test batch. For example, if finding
    opponents, `indices[i][j]` stores the index in the training data of the example
    with the `j`-th highest influence score on the `i`-th example in the test batch.
    Similarly, the `influence_scores` field stores the actual influence scores, so that
    `influence_scores[i][j]` is the influence score of example `indices[i][j]` in the
    training data on example `i` of the test batch. Please see `TracInCPBase.influence`
    for more details.
    """

    indices: Tensor
    influence_scores: Tensor


class TracInCPBase(DataInfluence):
    """
    To implement the `influence` method, classes inheriting from `TracInCPBase` will
    separately implement the private `_self_influence`, `_get_k_most_influential`,
    and `_influence` methods. The public `influence` method is a wrapper for these
    private methods.
    """

    def __init__(
        self,
        model: Module,
        train_dataset: Union[Dataset, DataLoader],
        checkpoints: Union[str, List[str], Iterator],
        checkpoints_load_func: Callable = _load_flexible_state_dict,
        loss_fn: Optional[Union[Module, Callable]] = None,
        batch_size: Union[int, None] = 1,
    ) -> None:
        r"""
        Args:

            model (torch.nn.Module): An instance of pytorch model. This model should
                    define all of its layers as attributes of the model.
            train_dataset (torch.utils.data.Dataset or torch.utils.data.DataLoader):
                    In the `influence` method, we either compute the influence score of
                    training examples on examples in a test batch, or self influence
                    scores for those training examples, depending on which mode is used.
                    This argument represents the training dataset containing those
                    training examples. In order to compute those influence scores, we
                    will create a Pytorch DataLoader yielding batches of training
                    examples that is then used for processing. If this argument is
                    already a Pytorch Dataloader, that DataLoader can be directly
                    used for processing. If it is instead a Pytorch Dataset, we will
                    create a DataLoader using it, with batch size specified by
                    `batch_size`. For efficiency purposes, the batch size of the
                    DataLoader used for processing should be as large as possible, but
                    not too large, so that certain intermediate quantities created
                    from a batch still fit in memory. Therefore, if
                    `train_dataset` is a Dataset, `batch_size` should be large.
                    If `train_dataset` was already a DataLoader to begin with,
                    it should have been constructed to have a large batch size. It is
                    assumed that the Dataloader (regardless of whether it is created
                    from a Pytorch Dataset or not) yields tuples. For a `batch` that is
                    yielded, of length `L`, it is assumed that the forward function of
                    `model` accepts `L-1` arguments, and the last element of `batch` is
                    the label. In other words, `model(*batch[:-1])` gives the output of
                    `model`, and `batch[-1]` are the labels for the batch.
            checkpoints (str, list[str], or Iterator): Either the directory of the
                    path to store and retrieve model checkpoints, a list of
                    filepaths with checkpoints from which to load, or an iterator which
                    returns objects from which to load checkpoints.
            checkpoints_load_func (Callable, optional): The function to load a saved
                    checkpoint into a model to update its parameters, and get the
                    learning rate if it is saved. By default uses a utility to load a
                    model saved as a state dict.
                    Default: _load_flexible_state_dict
            layers (list[str] or None, optional): A list of layer names for which
                    gradients should be computed. If `layers` is None, gradients will
                    be computed for all layers. Otherwise, they will only be computed
                    for the layers specified in `layers`.
                    Default: None
            loss_fn (Callable, optional): The loss function applied to model.
                    Default: None
            batch_size (int or None, optional): Batch size of the DataLoader created to
                    iterate through `train_dataset`, if it is a Dataset.
                    `batch_size` should be chosen as large as possible so that certain
                    intermediate quantities created from a batch still fit in memory.
                    Specific implementations of `TracInCPBase` will detail the size of
                    the intermediate quantities. `batch_size` must be an int if
                    `train_dataset` is a Dataset. If `train_dataset`
                    is a DataLoader, then `batch_size` is ignored as an argument.
                    Default: 1
        """

        self.model = model

        if isinstance(checkpoints, str):
            self.checkpoints = AV.sort_files(glob.glob(join(checkpoints, "*")))
        elif isinstance(checkpoints, List) and isinstance(checkpoints[0], str):
            self.checkpoints = AV.sort_files(checkpoints)
        else:
            self.checkpoints = list(checkpoints)  # cast to avoid mypy error
        if isinstance(self.checkpoints, List):
            assert len(self.checkpoints) > 0, "No checkpoints saved!"

        self.checkpoints_load_func = checkpoints_load_func
        self.loss_fn = loss_fn
        self.batch_size = batch_size

        if not isinstance(train_dataset, DataLoader):
            assert isinstance(batch_size, int), (
                "since the `train_dataset` argument was a `Dataset`, "
                "`batch_size` must be an int."
            )
            self.train_dataloader = DataLoader(train_dataset, batch_size, shuffle=False)
        else:
            self.train_dataloader = train_dataset

        self.train_dataloader_len: Optional[int] = None
        try:

            # since we will calculate the number of batches in
            # `self.train_dataloader` whenever we use progress bar, calculate
            # it once in initialization, for re-use.
            self.train_dataloader_len = len(self.train_dataloader)
        except TypeError:
            warnings.warn(
                "Unable to determine the number of batches in training dataset "
                "`train_dataset`. Therefore, if showing the progress of computations, "
                "only the number of batches processed can be displayed, and not the "
                "percentage completion of the computation, nor any time estimates."
            )

    @abstractmethod
    def self_influence(
        self,
        inputs_dataset: Union[Tuple[Any, ...], DataLoader],
        show_progress: bool = False,
    ) -> Tensor:
        """
        Computes self influence scores for the examples in `inputs_dataset`, which is
        either a single batch or a Pytorch `DataLoader` that yields batches. Therefore,
        the computed self influence scores are *not* for the examples in training
        dataset `train_dataset` (unlike when computing self influence scores using the
        `influence` method). Note that if `inputs_dataset` is a single batch, this
        will call `model` on that single batch, and if `inputs_dataset` yields
        batches, this will call `model` on each batch that is yielded. Therefore,
        please ensure that for both cases, the batch(es) that `model` is called
        with are not too large, so that there will not be an out-of-memory error.

        Args:
            batches (tuple or DataLoader): Either a single tuple of any, or a
                    `DataLoader`, where each batch yielded is a tuple of any. In
                    either case, the tuple represents a single batch, where the last
                    element is assumed to be the labels for the batch. That is,
                    `model(*batch[0:-1])` produces the output for `model`,
                    and `batch[-1]` are the labels, if any. This is the same
                    assumption made for each batch yielded by training dataset
                    `train_dataset`. Please see documentation for the
                    `train_dataset` argument to `TracInCP.__init__` for
                    more details on the assumed structure of a batch.
            show_progress (bool, optional): Computation of self influence scores can
                    take a long time if `inputs_dataset` represents many examples. If
                    `show_progress` is true, the progress of this computation will be
                    displayed. In more detail, this computation will iterate over all
                    checkpoints (provided as the `checkpoints` initialization argument)
                    in an outer loop, and iterate over all batches that
                    `inputs_dataset` represents in an inner loop. Therefore, the
                    total number of (checkpoint, batch) combinations that need to be
                    iterated over is
                    (# of checkpoints x # of batches that `inputs_dataset` represents).
                    If `show_progress` is True, the total progress of both the outer
                    iteration over checkpoints and the inner iteration over batches is
                    displayed. It will try to use tqdm if available for advanced
                    features (e.g. time estimation). Otherwise, it will fallback to a
                    simple output of progress.
                    Default: False

        Returns:
            self_influence_scores (Tensor): This is a 1D tensor containing the self
                    influence scores of all examples in `inputs_dataset`, regardless of
                    whether it represents a single batch or a `DataLoader` that yields
                    batches.
        """
        pass

    @abstractmethod
    def _get_k_most_influential(
        self,
        inputs: Tuple[Any, ...],
        targets: Optional[Tensor] = None,
        k: int = 5,
        proponents: bool = True,
        show_progress: bool = False,
    ) -> KMostInfluentialResults:
        r"""
        Args:

            inputs (tuple[Any, ...]): A tuple that represents a batch of examples. It
                    does not represent labels, which are passed as `targets`.
            targets (Tensor, optional): If computing influence scores on a loss
                    function, these are the labels corresponding to the batch `inputs`.
                    Default: None
            k (int, optional): The number of proponents or opponents to return per test
                    example.
                    Default: 5
            proponents (bool, optional): Whether seeking proponents (`proponents=True`)
                    or opponents (`proponents=False`)
                    Default: True
            show_progress (bool, optional): To compute the proponents (or opponents)
                    for the batch of examples, we perform computation for each batch in
                    training dataset `train_dataset`, If `show_progress` is
                    true, the progress of this computation will be displayed. In
                    particular, the number of batches for which the computation has
                    been performed will be displayed. It will try to use tqdm if
                    available for advanced features (e.g. time estimation). Otherwise,
                    it will fallback to a simple output of progress.
                    Default: False

        Returns:
            (indices, influence_scores) (namedtuple): `indices` is a torch.long Tensor
                    that contains the indices of the proponents (or opponents) for each
                    test example. Its dimension is `(inputs_batch_size, k)`, where
                    `inputs_batch_size` is the number of examples in `inputs`. For
                    example, if `proponents==True`, `indices[i][j]` is the index of the
                    example in training dataset `train_dataset` with the
                    k-th highest influence score for the j-th example in `inputs`.
                    `indices` is a `torch.long` tensor so that it can directly be used
                    to index other tensors. Each row of `influence_scores` contains the
                    influence scores for a different test example, in sorted order. In
                    particular, `influence_scores[i][j]` is the influence score of
                    example `indices[i][j]` in training dataset `train_dataset`
                    on example `i` in the test batch represented by `inputs` and
                    `targets`.
        """
        pass

    @abstractmethod
    def _influence(
        self,
        inputs: Tuple[Any, ...],
        targets: Optional[Tensor] = None,
        show_progress: bool = False,
    ) -> Tensor:
        r"""
        Args:

            inputs (tuple[Any, ...]): A batch of examples. Does not represent labels,
                    which are passed as `targets`. The assumption is that
                    `model(*inputs)` produces the predictions for the batch.
            targets (Tensor, optional): If computing influence scores on a loss
                    function, these are the labels corresponding to the batch
                    `inputs`.
                    Default: None

        Returns:
            influence_scores (Tensor): Influence scores over the entire
                    training dataset `train_dataset`. Dimensionality is
                    (inputs_batch_size, src_dataset_size). For example:
                    influence_scores[i][j] = the influence score for the j-th training
                    example to the i-th input example.
            show_progress (bool, optional): To compute the influence of examples in
                    training dataset `train_dataset`, we compute the influence
                    of each batch. If `show_progress` is true, the progress of this
                    computation will be displayed. In particular, the number of batches
                    for which influence has been computed will be displayed. It will
                    try to use tqdm if available for advanced features (e.g. time
                    estimation). Otherwise, it will fallback to a simple output of
                    progress.
                    Default: False
        """
        pass

    @abstractmethod
    def influence(  # type: ignore[override]
        self,
        inputs: Any = None,
        targets: Optional[Tensor] = None,
        k: Optional[int] = None,
        proponents: bool = True,
        unpack_inputs: bool = True,
        show_progress: bool = False,
    ) -> Union[Tensor, KMostInfluentialResults]:
        r"""
        This is the key method of this class, and can be run in 3 different modes,
        where the mode that is run depends on the arguments passed to this method:

        - self influence mode: This mode is used if `inputs` is None. This mode
          computes the self influence scores for every example in
          the training dataset `train_dataset`.
        - influence score mode: This mode is used if `inputs` is not None, and `k` is
          None. This mode computes the influence score of every example in
          training dataset `train_dataset` on every example in the test
          batch represented by `inputs` and `targets`.
        - k-most influential mode: This mode is used if `inputs` is not None, and
          `k` is not None, and an int. This mode computes the proponents or
          opponents of every example in the test batch represented by `inputs`
          and `targets`. In particular, for each test example in the test batch,
          this mode computes its proponents (resp. opponents), which are the
          indices in the training dataset `train_dataset` of the training
          examples with the `k` highest (resp. lowest) influence scores on the
          test example. Proponents are computed if `proponents` is True.
          Otherwise, opponents are computed. For each test example, this method
          also returns the actual influence score of each proponent (resp.
          opponent) on the test example.

        Args:

            inputs (Any, optional): If not provided or `None`, the self influence mode
                    will be run. Otherwise, `inputs` is the test batch that will be
                    used when running in either influence score or k-most influential
                    mode. If the argument `unpack_inputs` is False, the
                    assumption is that `model(inputs)` produces the predictions
                    for a batch, and `inputs` can be of any type. Otherwise if the
                    argument `unpack_inputs` is True, the assumption is that
                    `model(*inputs)` produces the predictions for a batch, and
                    `inputs` will need to be a tuple. In other words, `inputs` will be
                    unpacked as an argument when passing to `model`.
                    Default: None
            targets (Tensor, optional): If computing influence scores on a loss
                    function, these are the labels corresponding to the batch `inputs`.
                    Default: None
            k (int, optional): If not provided or `None`, the influence score mode will
                    be run. Otherwise, the k-most influential mode will be run,
                    and `k` is the number of proponents / opponents to return per
                    example in the test batch.
                    Default: None
            proponents (bool, optional): Whether seeking proponents (`proponents=True`)
                    or opponents (`proponents=False`), if running in k-most influential
                    mode.
                    Default: True
            unpack_inputs (bool, optional): Whether to unpack the `inputs` argument to
                    when passing it to `model`, if `inputs` is a tuple (no unpacking
                    done otherwise).
                    Default: True
            show_progress (bool, optional): For all modes, computation of results
                    requires "training dataset computations": computations for each
                    batch in the training dataset `train_dataset`, which may
                    take a long time. If `show_progress` is true, the progress of
                    "training dataset computations" will be displayed. In particular,
                    the number of batches for which computations have been performed
                    will be displayed. It will try to use tqdm if available for
                    advanced features (e.g. time estimation). Otherwise, it will
                    fallback to a simple output of progress.
                    Default: False

        Returns:
            The return value of this method depends on which mode is run.

            - self influence mode: if this mode is run (`inputs` is None), returns a 1D
              tensor of self influence scores over training dataset
              `train_dataset`. The length of this tensor is the number of
              examples in `train_dataset`, regardless of whether it is a
              Dataset or DataLoader.
            - influence score mode: if this mode is run (`inputs is not None, `k` is
              None), returns a 2D tensor `influence_scores` of shape
              `(input_size, train_dataset_size)`, where `input_size` is
              the number of examples in the test batch, and
              `train_dataset_size` is the number of examples in
              training dataset `train_dataset`. In other words,
              `influence_scores[i][j]` is the influence score of the `j`-th
              example in `train_dataset` on the `i`-th example in the
              test batch.
            - k-most influential mode: if this mode is run (`inputs` is not None,
              `k` is an int), returns a namedtuple `(indices, influence_scores)`.
              `indices` is a 2D tensor of shape `(input_size, k)`, where
              `input_size` is the number of examples in the test batch. If
              computing proponents (resp. opponents), `indices[i][j]` is the
              index in training dataset `train_dataset` of the example
              with the `j`-th highest (resp. lowest) influence score (out of the
              examples in `train_dataset`) on the `i`-th example in the
              test batch. `influence_scores` contains the corresponding influence
              scores. In particular, `influence_scores[i][j]` is the influence
              score of example `indices[i][j]` in `train_dataset` on
              example `i` in the test batch represented by `inputs` and
              `targets`.
        """
        pass

    @classmethod
    def get_name(cls: Type["TracInCPBase"]) -> str:
        r"""
        Create readable class name.  Due to the nature of the names of `TracInCPBase`
        subclasses, simplies returns the class name.  For example, for a class called
        TracInCP, we return the string TracInCP.

        Returns:
            name (str): a readable class name
        """
        return cls.__name__


def _influence_route_to_helpers(
    influence_instance: TracInCPBase,
    inputs: Any = None,
    targets: Optional[Tensor] = None,
    k: Optional[int] = None,
    proponents: bool = True,
    unpack_inputs: bool = True,
    show_progress: bool = False,
) -> Union[Tensor, KMostInfluentialResults]:
    """
    This is a helper function called by `TracInCP.influence` and
    `TracInCPFast.influence`. Those methods share a common logic in that they assume
    an instance of their respective classes implement 3 private methods
    (`_self_influence`, `_influence`, `_get_k_most_influential`), and the logic of
    which private method to call is common, as described in the documentation of the
    `influence` method. The arguments and return values of this function are the exact
    same as the `influence` method. Note that `influence_instance` refers to the
    instance for which the `influence` method was called.
    """
    _inputs = _format_inputs(inputs, unpack_inputs)

    if inputs is None:
        return influence_instance.self_influence(
            influence_instance.train_dataloader,
            show_progress,
        )
    elif k is None:
        return influence_instance._influence(_inputs, targets, show_progress)
    else:
        return influence_instance._get_k_most_influential(
            _inputs, targets, k, proponents, show_progress
        )


class TracInCP(TracInCPBase):
    def __init__(
        self,
        model: Module,
        train_dataset: Union[Dataset, DataLoader],
        checkpoints: Union[str, List[str], Iterator],
        checkpoints_load_func: Callable = _load_flexible_state_dict,
        layers: Optional[List[str]] = None,
        loss_fn: Optional[Union[Module, Callable]] = None,
        batch_size: Union[int, None] = 1,
        sample_wise_grads_per_batch: bool = False,
    ) -> None:
        r"""
        Args:

            model (torch.nn.Module): An instance of pytorch model. This model should
                    define all of its layers as attributes of the model.
            train_dataset (torch.utils.data.Dataset or torch.utils.data.DataLoader):
                    In the `influence` method, we either compute the influence score of
                    training examples on examples in a test batch, or self influence
                    scores for those training examples, depending on which mode is used.
                    This argument represents the training dataset containing those
                    training examples. In order to compute those influence scores, we
                    will create a Pytorch DataLoader yielding batches of training
                    examples that is then used for processing. If this argument is
                    already a Pytorch Dataloader, that DataLoader can be directly
                    used for processing. If it is instead a Pytorch Dataset, we will
                    create a DataLoader using it, with batch size specified by
                    `batch_size`. For efficiency purposes, the batch size of the
                    DataLoader used for processing should be as large as possible, but
                    not too large, so that certain intermediate quantities created
                    from a batch still fit in memory. Therefore, if
                    `train_dataset` is a Dataset, `batch_size` should be large.
                    If `train_dataset` was already a DataLoader to begin with,
                    it should have been constructed to have a large batch size. It is
                    assumed that the Dataloader (regardless of whether it is created
                    from a Pytorch Dataset or not) yields tuples. For a `batch` that is
                    yielded, of length `L`, it is assumed that the forward function of
                    `model` accepts `L-1` arguments, and the last element of `batch` is
                    the label. In other words, `model(*batch[:-1])` gives the output of
                    `model`, and `batch[-1]` are the labels for the batch.
            checkpoints (str, list[str], or Iterator): Either the directory of the
                    path to store and retrieve model checkpoints, a list of
                    filepaths with checkpoints from which to load, or an iterator which
                    returns objects from which to load checkpoints.
            checkpoints_load_func (Callable, optional): The function to load a saved
                    checkpoint into a model to update its parameters, and get the
                    learning rate if it is saved. By default uses a utility to load a
                    model saved as a state dict.
                    Default: _load_flexible_state_dict
            layers (list[str] or None, optional): A list of layer names for which
                    gradients should be computed. If `layers` is None, gradients will
                    be computed for all layers. Otherwise, they will only be computed
                    for the layers specified in `layers`.
                    Default: None
            loss_fn (Callable, optional): The loss function applied to model. There
                    are two options for the return type of `loss_fn`. First, `loss_fn`
                    can be a "per-example" loss function - returns a 1D Tensor of
                    losses for each example in a batch. `nn.BCELoss(reduction="none")`
                    would be an "per-example" loss function. Second, `loss_fn` can be
                    a "reduction" loss function that reduces the per-example losses,
                    in a batch, and returns a single scalar Tensor. For this option,
                    the reduction must be the *sum* or the *mean* of the per-example
                    losses. For instance, `nn.BCELoss(reduction="sum")` is acceptable.
                    Note for the first option, the `sample_wise_grads_per_batch`
                    argument must be False, and for the second option,
                    `sample_wise_grads_per_batch` must be True.  Also note that for
                    the second option, if `loss_fn` has no "reduction" attribute,
                    the implementation assumes that the reduction is the *sum* of the
                    per-example losses.  If this is not the case, i.e. the reduction
                    is the *mean*, please set the "reduction" attribute of `loss_fn`
                    to "mean", i.e. `loss_fn.reduction = "mean"`.
                    Default: None
            batch_size (int or None, optional): Batch size of the DataLoader created to
                    iterate through `train_dataset`, if it is a Dataset.
                    `batch_size` should be chosen as large as possible so that certain
                    intermediate quantities created from a batch still fit in memory.
                    Specific implementations of `TracInCPBase` will detail the size of
                    the intermediate quantities. `batch_size` must be an int if
                    `train_dataset` is a Dataset. If `train_dataset`
                    is a DataLoader, then `batch_size` is ignored as an argument.
                    Default: 1
            sample_wise_grads_per_batch (bool, optional): PyTorch's native gradient
                    computations w.r.t. model parameters aggregates the results for a
                    batch and does not allow to access sample-wise gradients w.r.t.
                    model parameters. This forces us to iterate over each sample in
                    the batch if we want sample-wise gradients which is computationally
                    inefficient. We offer an implementation of batch-wise gradient
                    computations w.r.t. to model parameters which is computationally
                    more efficient. This implementation can be enabled by setting the
                    `sample_wise_grad_per_batch` argument to `True`, and should be
                    enabled if and only if the `loss_fn` argument is a "reduction" loss
                    function. For example, `nn.BCELoss(reduction="sum")` would be a
                    valid `loss_fn` if this implementation is enabled (see
                    documentation for `loss_fn` for more details). Note that our
                    current implementation enables batch-wise gradient computations
                    only for a limited number of PyTorch nn.Modules: Conv2D and Linear.
                    This list will be expanded in the near future.  Therefore, please
                    do not enable this implementation if gradients will be computed
                    for other kinds of layers.
                    Default: False
        """

        TracInCPBase.__init__(
            self,
            model,
            train_dataset,
            checkpoints,
            checkpoints_load_func,
            loss_fn,
            batch_size,
        )

        self.sample_wise_grads_per_batch = sample_wise_grads_per_batch

        # If we are able to access the reduction used by `loss_fn`, we check whether
        # the reduction is compatible with `sample_wise_grads_per_batch`
        if isinstance(loss_fn, Module) and hasattr(
            loss_fn, "reduction"
        ):  # TODO: allow loss_fn to be Callable
            if self.sample_wise_grads_per_batch:
                assert loss_fn.reduction in ["sum", "mean"], (
                    'reduction for `loss_fn` must be "sum" or "mean" when '
                    "`sample_wise_grads_per_batch` is True"
                )
                self.reduction_type = str(loss_fn.reduction)
            else:
                assert loss_fn.reduction == "none", (
                    'reduction for `loss_fn` must be "none" when '
                    "`sample_wise_grads_per_batch` is False"
                )
        else:
            # if we are unable to access the reduction used by `loss_fn`, we warn
            # the user about the assumptions we are making regarding the reduction
            # used by `loss_fn`
            if self.sample_wise_grads_per_batch:
                warnings.warn(
                    'Since `loss_fn` has no "reduction" attribute, and '
                    "`sample_wise_grads_per_batch` is True, the implementation assumes "
                    'that `loss_fn` is a "reduction" loss function that reduces the '
                    "per-example losses by taking their *sum*. If `loss_fn` "
                    "instead reduces the per-example losses by taking their mean, "
                    'please set the reduction attribute of `loss_fn` to "mean", i.e. '
                    '`loss_fn.reduction = "mean"`. Note that if '
                    "`sample_wise_grads_per_batch` is True, the implementation "
                    "assumes the reduction is either a sum or mean reduction."
                )
                self.reduction_type = "sum"
            else:
                warnings.warn(
                    'Since `loss_fn` has no "reduction" attribute, and '
                    "`sample_wise_grads_per_batch` is False, the implementation "
                    'assumes that `loss_fn` is a "per-example" loss function (see '
                    "documentation for `loss_fn` for details).  Please ensure that "
                    "this is the case."
                )

        r"""
        TODO: Either restore model state after done (would have to place functionality
        within influence to restore after every influence call)? or make a copy so that
        changes to grad_requires aren't persistent after using TracIn.
        """
        if layers is not None:
            assert isinstance(layers, List), "`layers` should be a list!"
            assert len(layers) > 0, "`layers` cannot be empty!"
            assert isinstance(
                layers[0], str
            ), "`layers` should contain str layer names."
            layerstr = " ".join(layers)
            gradset = False
            for layer in layers:
                for name, param in model.named_parameters():
                    param.requires_grad = False
                    if name in layerstr or layer in name:
                        param.requires_grad = True
                        gradset = True
            assert gradset, "At least one parameter of network must require gradient."

    @log_usage()
    def influence(  # type: ignore[override]
        self,
        inputs: Any = None,
        targets: Optional[Tensor] = None,
        k: Optional[int] = None,
        proponents: bool = True,
        unpack_inputs: bool = True,
        show_progress: bool = False,
    ) -> Union[Tensor, KMostInfluentialResults]:
        r"""
        This is the key method of this class, and can be run in 3 different modes,
        where the mode that is run depends on the arguments passed to this method:

        - self influence mode: This mode is used if `inputs` is None. This mode
          computes the self influence scores for every example in
          the training dataset `train_dataset`.
        - influence score mode: This mode is used if `inputs` is not None, and `k` is
          None. This mode computes the influence score of every example in
          training dataset `train_dataset` on every example in the test
          batch represented by `inputs` and `targets`.
        - k-most influential mode: This mode is used if `inputs` is not None, and
          `k` is not None, and an int. This mode computes the proponents or
          opponents of every example in the test batch represented by `inputs`
          and `targets`. In particular, for each test example in the test batch,
          this mode computes its proponents (resp. opponents), which are the
          indices in the training dataset `train_dataset` of the training
          examples with the `k` highest (resp. lowest) influence scores on the
          test example. Proponents are computed if `proponents` is True.
          Otherwise, opponents are computed. For each test example, this method
          also returns the actual influence score of each proponent (resp.
          opponent) on the test example.

        Args:

            inputs (Any, optional): If not provided or `None`, the self influence mode
                    will be run. Otherwise, `inputs` is the test batch that will be
                    used when running in either influence score or k-most influential
                    mode. If the argument `unpack_inputs` is False, the
                    assumption is that `model(inputs)` produces the predictions
                    for a batch, and `inputs` can be of any type. Otherwise if the
                    argument `unpack_inputs` is True, the assumption is that
                    `model(*inputs)` produces the predictions for a batch, and
                    `inputs` will need to be a tuple. In other words, `inputs` will be
                    unpacked as an argument when passing to `model`.
                    Default: None
            targets (Tensor, optional): If computing influence scores on a loss
                    function, these are the labels corresponding to the batch `inputs`.
                    Default: None
            k (int, optional): If not provided or `None`, the influence score mode will
                    be run. Otherwise, the k-most influential mode will be run,
                    and `k` is the number of proponents / opponents to return per
                    example in the test batch.
                    Default: None
            proponents (bool, optional): Whether seeking proponents (`proponents=True`)
                    or opponents (`proponents=False`), if running in k-most influential
                    mode.
                    Default: True
            unpack_inputs (bool, optional): Whether to unpack the `inputs` argument to
                    when passing it to `model`, if `inputs` is a tuple (no unpacking
                    done otherwise).
                    Default: True
            show_progress (bool, optional): For all modes, computation of results
                    requires "training dataset computations": computations for each
                    batch in the training dataset `train_dataset`, which may
                    take a long time. If `show_progress` is true, the progress of
                    "training dataset computations" will be displayed. It will try to
                    use tqdm if available for advanced features (e.g. time estimation).
                    Otherwise, it will fallback to a simple output of progress.
                    Default: False

        Returns:
            The return value of this method depends on which mode is run.

            - self influence mode: if this mode is run (`inputs` is None), returns a 1D
              tensor of self influence scores over training dataset
              `train_dataset`. The length of this tensor is the number of
              examples in `train_dataset`, regardless of whether it is a
              Dataset or DataLoader.
            - influence score mode: if this mode is run (`inputs is not None, `k` is
              None), returns a 2D tensor `influence_scores` of shape
              `(input_size, train_dataset_size)`, where `input_size` is
              the number of examples in the test batch, and
              `train_dataset_size` is the number of examples in
              training dataset `train_dataset`. In other words,
              `influence_scores[i][j]` is the influence score of the `j`-th
              example in `train_dataset` on the `i`-th example in the
              test batch.
            - k-most influential mode: if this mode is run (`inputs` is not None,
              `k` is an int), returns a namedtuple `(indices, influence_scores)`.
              `indices` is a 2D tensor of shape `(input_size, k)`, where
              `input_size` is the number of examples in the test batch. If
              computing proponents (resp. opponents), `indices[i][j]` is the
              index in training dataset `train_dataset` of the example
              with the `j`-th highest (resp. lowest) influence score (out of the
              examples in `train_dataset`) on the `i`-th example in the
              test batch. `influence_scores` contains the corresponding influence
              scores. In particular, `influence_scores[i][j]` is the influence
              score of example `indices[i][j]` in `train_dataset` on
              example `i` in the test batch represented by `inputs` and
              `targets`.
        """
        return _influence_route_to_helpers(
            self,
            inputs,
            targets,
            k,
            proponents,
            unpack_inputs,
            show_progress,
        )

    def _influence_batch_tracincp(
        self,
        inputs: Tuple[Any, ...],
        targets: Optional[Tensor],
        batch: Tuple[Any, ...],
    ):
        """
        computes influence scores for a single training batch
        """

        def get_checkpoint_contribution(checkpoint):

            assert (
                checkpoint is not None
            ), "None returned from `checkpoints`, cannot load."

            learning_rate = self.checkpoints_load_func(self.model, checkpoint)

            input_jacobians = self._basic_computation_tracincp(
                inputs,
                targets,
            )

            return (
                _gradient_dot_product(
                    input_jacobians,
                    self._basic_computation_tracincp(batch[0:-1], batch[-1]),
                )
                * learning_rate
            )

        batch_tracin_scores = get_checkpoint_contribution(self.checkpoints[0])

        for checkpoint in self.checkpoints[1:]:
            batch_tracin_scores += get_checkpoint_contribution(checkpoint)

        return batch_tracin_scores

    def _influence(
        self,
        inputs: Tuple[Any, ...],
        targets: Optional[Tensor] = None,
        show_progress: bool = False,
    ) -> Tensor:
        r"""
        Computes the influence of examples in training dataset `train_dataset`
        on the examples in the test batch represented by `inputs` and `targets`.
        This implementation does not require knowing the number of training examples
        in advance. Instead, the number of training examples is inferred from the
        output of `self._basic_computation_tracincp`.

        Args:

            inputs (tuple[Any, ...]): A test batch of examples. Does not represent
                    labels, which are passed as `targets`. The assumption is that
                    `model(*inputs)` produces the predictions for the batch.
            targets (Tensor, optional): If computing influence scores on a loss
                    function, these are the labels corresponding to the batch `inputs`.
                    Default: None
            show_progress (bool, optional): To compute the influence of examples in
                    training dataset `train_dataset`, we compute the influence
                    of each batch. If `show_progress` is true, the progress of this
                    computation will be displayed. In particular, the number of batches
                    for which influence has been computed will be displayed. It will
                    try to use tqdm if available for advanced features (e.g. time
                    estimation). Otherwise, it will fallback to a simple output of
                    progress.
                    Default: False

        Returns:
            influence_scores (Tensor): Influence scores from the TracInCP method.
            Its shape is `(input_size, train_dataset_size)`, where `input_size`
            is the number of examples in the test batch, and
            `train_dataset_size` is the number of examples in
            training dataset `train_dataset`. For example:
            `influence_scores[i][j]` is the influence score for the j-th training
            example to the i-th input example.
        """
        train_dataloader = self.train_dataloader

        if show_progress:
            train_dataloader = progress(
                train_dataloader,
                desc=(
                    f"Using {self.get_name()} to compute "
                    "influence for training batches"
                ),
                total=self.train_dataloader_len,
            )

        return torch.cat(
            [
                self._influence_batch_tracincp(inputs, targets, batch)
                for batch in train_dataloader
            ],
            dim=1,
        )

    def _get_k_most_influential(
        self,
        inputs: Tuple[Any, ...],
        targets: Optional[Tensor] = None,
        k: int = 5,
        proponents: bool = True,
        show_progress: bool = False,
    ) -> KMostInfluentialResults:
        r"""
        Args:

            inputs (tuple[Any, ...]): A tuple that represents a batch of examples. It
                    does not represent labels, which are passed as `targets`.
            targets (Tensor, optional): If computing influence scores on a loss
                    function, these are the labels corresponding to the batch `inputs`.
                    Default: None
            k (int, optional): The number of proponents or opponents to return per test
                    example.
                    Default: 5
            proponents (bool, optional): Whether seeking proponents (`proponents=True`)
                    or opponents (`proponents=False`)
                    Default: True
            show_progress (bool, optional): To compute the proponents (or opponents)
                    for the batch of examples, we perform computation for each batch in
                    training dataset `train_dataset`, If `show_progress` is
                    true, the progress of this computation will be displayed. In
                    particular, the number of batches for which the computation has
                    been performed will be displayed. It will try to use tqdm if
                    available for advanced features (e.g. time estimation). Otherwise,
                    it will fallback to a simple output of progress.
                    Default: False

        Returns:
            (indices, influence_scores) (namedtuple): `indices` is a torch.long Tensor
                    that contains the indices of the proponents (or opponents) for each
                    test example. Its dimension is `(inputs_batch_size, k)`, where
                    `inputs_batch_size` is the number of examples in `inputs`. For
                    example, if `proponents==True`, `indices[i][j]` is the index of the
                    example in training dataset `train_dataset` with the
                    k-th highest influence score for the j-th example in `inputs`.
                    `indices` is a `torch.long` tensor so that it can directly be used
                    to index other tensors. Each row of `influence_scores` contains the
                    influence scores for a different test example, in sorted order. In
                    particular, `influence_scores[i][j]` is the influence score of
                    example `indices[i][j]` in training dataset `train_dataset`
                    on example `i` in the test batch represented by `inputs` and
                    `targets`.
        """
        desc = (
            None
            if not show_progress
            else (
                (
                    f"Using {self.get_name()} to perform computation for "
                    f'getting {"proponents" if proponents else "opponents"}. '
                    "Processing training batches"
                )
            )
        )
        return KMostInfluentialResults(
            *_get_k_most_influential_helper(
                self.train_dataloader,
                self._influence_batch_tracincp,
                inputs,
                targets,
                k,
                proponents,
                show_progress,
                desc,
            )
        )

    def _self_influence_by_checkpoints(
        self,
        inputs_dataset: Union[Tuple[Any, ...], DataLoader],
        show_progress: bool = False,
    ) -> Tensor:
        """
        Computes self influence scores for the examples in `inputs_dataset`, which is
        either a single batch or a Pytorch `DataLoader` that yields batches. Therefore,
        the computed self influence scores are *not* for the examples in training
        dataset `train_dataset` (unlike when computing self influence scores using the
        `influence` method). Note that if `inputs_dataset` is a single batch, this
        will call `model` on that single batch, and if `inputs_dataset` yields
        batches, this will call `model` on each batch that is yielded. Therefore,
        please ensure that for both cases, the batch(es) that `model` is called
        with are not too large, so that there will not be an out-of-memory error. This
        implementation performs an outer iteration over checkpoints, and an inner
        iteration over all batches that `inputs_dataset` represents. The pros of this
        implementation are that the checkpoints do not need to be loaded too many
        times.

        Args:
            batches (tuple or DataLoader): Either a single tuple of any, or a
                    `DataLoader`, where each batch yielded is a tuple of any. In
                    either case, the tuple represents a single batch, where the last
                    element is assumed to be the labels for the batch. That is,
                    `model(*batch[0:-1])` produces the output for `model`,
                    and `batch[-1]` are the labels, if any. This is the same
                    assumption made for each batch yielded by training dataset
                    `train_dataset`. Please see documentation for the
                    `train_dataset` argument to `TracInCP.__init__` for
                    more details on the assumed structure of a batch.
            show_progress (bool, optional): Computation of self influence scores can
                    take a long time if `inputs_dataset` represents many examples. If
                    `show_progress` is true, the progress of this computation will be
                    displayed. In more detail, this computation will iterate over all
                    checkpoints (provided as the `checkpoints` initialization argument)
                    in an outer loop, and iterate over all batches that
                    `inputs_dataset` represents in an inner loop. Thus if
                    `show_progress` is True, the progress of both the outer
                    iteration and the inner iterations will be displayed. To show
                    progress, it will try to use tqdm if available for advanced
                    features (e.g. time estimation). Otherwise, it will fallback to a
                    simple output of progress.
                    Default: False

        Returns:
            self_influence_scores (Tensor): This is a 1D tensor containing the self
                    influence scores of all examples in `inputs_dataset`, regardless of
                    whether it represents a single batch or a `DataLoader` that yields
                    batches.
        """
        # If `inputs_dataset` is not a `DataLoader`, turn it into one.
        inputs_dataset = _format_inputs_dataset(inputs_dataset)

        # If `show_progress` is true, create an outer progress bar that keeps track of
        # how many checkpoints have been processed
        if show_progress:
            checkpoints_progress = progress(
                desc=(
                    f"Using {self.get_name()} to compute self "
                    "influence. Processing checkpoint"
                ),
                total=len(self.checkpoints),
            )
            # Try to determine length of inner progress bar if possible, with a default
            # of `None`.
            inputs_dataset_len = None
            try:
                inputs_dataset_len = len(inputs_dataset)
            except TypeError:
                warnings.warn(
                    "Unable to determine the number of batches in `inputs_dataset`. "
                    "Therefore, if showing the progress of the computation of self "
                    "influence scores, only the number of batches processed can be "
                    "displayed, and not the percentage completion of the computation, "
                    "nor any time estimates."
                )

        def get_checkpoint_contribution(checkpoint):
            # This function returns a 1D tensor representing the contribution to the
            # self influence score for the given checkpoint, for all batches in
            # `inputs_dataset`. The length of the 1D tensor is the total number of
            # examples in `inputs_dataset`.
            assert (
                checkpoint is not None
            ), "None returned from `checkpoints`, cannot load."

            learning_rate = self.checkpoints_load_func(self.model, checkpoint)

            # This will store a list of the contribution of the self influence score
            # from each batch. Each element is a 1D tensor of length batch_size - the
            # batch size of each batch in `inputs_dataset` (they do not need to be all
            # the same)
            checkpoint_contribution = []

            _inputs_dataset = inputs_dataset
            # If `show_progress` is true, create an inner progress bar that keeps track
            # of how many batches have been processed for the current checkpoint
            if show_progress:
                _inputs_dataset = progress(
                    inputs_dataset,
                    desc=(
                        f"Using {self.get_name()} to compute self "
                        "influence. Processing batch"
                    ),
                    total=inputs_dataset_len,
                )

            for batch in _inputs_dataset:

                layer_jacobians = self._basic_computation_tracincp(
                    batch[0:-1], batch[-1]
                )

                # Note that all variables in this function are for an entire batch.
                # Each `layer_jacobian` in `layer_jacobians` corresponds to a different
                # layer. `layer_jacobian` is the jacobian w.r.t to a given layer's
                # parameters. If the given layer's parameters are of shape *, then
                # `layer_jacobian` is of shape (batch_size, *). For each layer, we need
                # the squared jacobian for each example. So we square the jacobian and
                # sum over all dimensions except the 0-th (the batch dimension). We then
                # sum the contribution over all layers.
                checkpoint_contribution.append(
                    torch.sum(
                        torch.stack(
                            [
                                torch.sum(
                                    layer_jacobian.flatten(start_dim=1) ** 2, dim=1
                                )
                                for layer_jacobian in layer_jacobians
                            ],
                            dim=0,
                        ),
                        dim=0,
                    )
                    * learning_rate
                )

            # We concatenate the contributions from each batch into a single 1D tensor,
            # which represents the contributions for all batches in `inputs_dataset`

            if show_progress:
                checkpoints_progress.update()

            return torch.cat(checkpoint_contribution, dim=0)

        batches_self_tracin_scores = get_checkpoint_contribution(self.checkpoints[0])

        # The self influence score for all examples is the sum of contributions from
        # each checkpoint
        for checkpoint in self.checkpoints[1:]:
            batches_self_tracin_scores += get_checkpoint_contribution(checkpoint)

        return batches_self_tracin_scores

    def self_influence(
        self,
        inputs_dataset: Union[Tuple[Any, ...], DataLoader],
        show_progress: bool = False,
        outer_loop_by_checkpoints: bool = False,
    ) -> Tensor:
        """
        Computes self influence scores for the examples in `inputs_dataset`, which is
        either a single batch or a Pytorch `DataLoader` that yields batches. Therefore,
        the computed self influence scores are *not* for the examples in training
        dataset `train_dataset` (unlike when computing self influence scores using the
        `influence` method). Note that if `inputs_dataset` is a single batch, this
        will call `model` on that single batch, and if `inputs_dataset` yields
        batches, this will call `model` on each batch that is yielded. Therefore,
        please ensure that for both cases, the batch(es) that `model` is called
        with are not too large, so that there will not be an out-of-memory error.
        Internally, this computation requires iterating both over the batches in
        `inputs_dataset`, as well as different model checkpoints. There are two ways
        this iteration can be done. If `outer_loop_by_checkpoints` is False, the outer
        iteration will be over batches, and the inner iteration will be over
        checkpoints. This has the pro that displaying the progress of the computation
        is more intuitive, involving displaying the number of batches for which self
        influence scores have been computed. If `outer_loop_by_checkpoints` is True,
        the outer iteration will be over checkpoints, and the inner iteration will be
        over batches. This has the pro that the checkpoints do not need to be loaded
        for each batch. For large models, loading checkpoints can be time-intensive.

        Args:
            batches (tuple or DataLoader): Either a single tuple of any, or a
                    `DataLoader`, where each batch yielded is a tuple of any. In
                    either case, the tuple represents a single batch, where the last
                    element is assumed to be the labels for the batch. That is,
                    `model(*batch[0:-1])` produces the output for `model`,
                    and `batch[-1]` are the labels, if any. This is the same
                    assumption made for each batch yielded by training dataset
                    `train_dataset`. Please see documentation for the
                    `train_dataset` argument to `TracInCP.__init__` for
                    more details on the assumed structure of a batch.
            show_progress (bool, optional): Computation of self influence scores can
                    take a long time if `inputs_dataset` represents many examples. If
                    `show_progress`is true, the progress of this computation will be
                    displayed. In more detail, if `outer_loop_by_checkpoints` is False,
                    this computation will iterate over all batches in an outer loop.
                    Thus if `show_progress` is True, the number of batches for which
                    self influence scores have been computed will be displayed. If
                    `outer_loop_by_checkpoints` is True, this computation will iterate
                    over all checkpoints (provided as the `checkpoints` initialization
                    argument) in an outer loop, and iterate over all batches that
                    `inputs_dataset` represents in an inner loop. Thus if
                    `show_progress` is True, the progress of both the outer
                    iteration and the inner iterations will be displayed. To show
                    progress, it will try to use tqdm if available for advanced
                    features (e.g. time estimation). Otherwise, it will fallback to a
                    simple output of progress.
                    Default: False
            outer_loop_by_checkpoints (bool, optional): If performing an outer
                    iteration over checkpoints; see method description for more
                    details.
                    Default: False
        """
        if outer_loop_by_checkpoints:
            return self._self_influence_by_checkpoints(inputs_dataset, show_progress)
        return _self_influence_by_batches_helper(
            self._self_influence_by_checkpoints,
            self.get_name(),
            inputs_dataset,
            show_progress,
        )

    def _basic_computation_tracincp(
        self,
        inputs: Tuple[Any, ...],
        targets: Optional[Tensor] = None,
    ) -> Tuple[Tensor, ...]:
        """
        For instances of TracInCP, computation of influence scores or self influence
        scores repeatedly calls this function for different checkpoints
        and batches.

        Args:

            inputs (tuple[Any, ...]): A batch of examples, which could be a training
                    batch or test batch, depending which method is the caller. Does not
                    represent labels, which are passed as `targets`. The assumption is
                    that `model(*inputs)` produces the predictions for the batch.
            targets (tensor or None): If computing influence scores on a loss function,
                    these are the labels corresponding to the batch `inputs`.
        """
        if self.sample_wise_grads_per_batch:
            return _compute_jacobian_wrt_params_with_sample_wise_trick(
                self.model,
                inputs,
                targets,
                self.loss_fn,
                self.reduction_type,
            )
        return _compute_jacobian_wrt_params(
            self.model,
            inputs,
            targets,
            self.loss_fn,
        )
