import copy
import datetime
import warnings
from functools import partial
import grpc
import logging
import os
from pathlib import Path
from pickle import PicklingError
import pprint as pp
import traceback
from typing import (
    Any,
    Dict,
    Optional,
    Sequence,
    Union,
    Callable,
    Type,
    List,
    Mapping,
    TYPE_CHECKING,
)

from ray.air import CheckpointConfig
from ray.air._internal.uri_utils import URI
from ray.exceptions import RpcError
from ray.train._internal.storage import _use_storage_context, StorageContext
from ray.tune.error import TuneError
from ray.tune.registry import register_trainable, is_function_trainable
from ray.tune.result import _get_defaults_results_dir
from ray.tune.stopper import CombinedStopper, FunctionStopper, Stopper, TimeoutStopper
from ray.tune.syncer import SyncConfig
from ray.tune.utils import date_str
from ray.tune.utils.util import _resolve_storage_path, _split_remote_local_path
from ray.util import log_once

from ray.util.annotations import DeveloperAPI, Deprecated

if TYPE_CHECKING:
    import pyarrow.fs

    from ray.tune.experiment import Trial
    from ray.tune import PlacementGroupFactory


logger = logging.getLogger(__name__)


def _validate_log_to_file(log_to_file):
    """Validate ``train.RunConfig``'s ``log_to_file`` parameter. Return
    validated relative stdout and stderr filenames."""
    if not log_to_file:
        stdout_file = stderr_file = None
    elif isinstance(log_to_file, bool) and log_to_file:
        stdout_file = "stdout"
        stderr_file = "stderr"
    elif isinstance(log_to_file, str):
        stdout_file = stderr_file = log_to_file
    elif isinstance(log_to_file, Sequence):
        if len(log_to_file) != 2:
            raise ValueError(
                "If you pass a Sequence to `log_to_file` it has to have "
                "a length of 2 (for stdout and stderr, respectively). The "
                "Sequence you passed has length {}.".format(len(log_to_file))
            )
        stdout_file, stderr_file = log_to_file
    else:
        raise ValueError(
            "You can pass a boolean, a string, or a Sequence of length 2 to "
            "`log_to_file`, but you passed something else ({}).".format(
                type(log_to_file)
            )
        )
    return stdout_file, stderr_file


def _get_local_dir_with_expand_user(local_dir: Optional[str]) -> str:
    return os.path.abspath(os.path.expanduser(local_dir or _get_defaults_results_dir()))


def _get_dir_name(run, explicit_name: Optional[str], combined_name: str) -> str:
    # If the name has been set explicitly, we don't want to create
    # dated directories. The same is true for string run identifiers.
    if (
        int(os.environ.get("TUNE_DISABLE_DATED_SUBDIR", 0)) == 1
        or explicit_name
        or isinstance(run, str)
    ):
        dir_name = combined_name
    else:
        dir_name = "{}_{}".format(combined_name, date_str())
    return dir_name


@DeveloperAPI
class Experiment:
    """Tracks experiment specifications.

    Implicitly registers the Trainable if needed. The args here take
    the same meaning as the arguments defined `tune.py:run`.

    .. code-block:: python

        experiment_spec = Experiment(
            "my_experiment_name",
            my_func,
            stop={"mean_accuracy": 100},
            config={
                "alpha": tune.grid_search([0.2, 0.4, 0.6]),
                "beta": tune.grid_search([1, 2]),
            },
            resources_per_trial={
                "cpu": 1,
                "gpu": 0
            },
            num_samples=10,
            local_dir="~/ray_results",
            checkpoint_freq=10,
            max_failures=2)

    Args:
        TODO(xwjiang): Add the whole list.
        _experiment_checkpoint_dir: Internal use only. If present, use this
            as the root directory for experiment checkpoint. If not present,
            the directory path will be deduced from trainable name instead.
    """

    # Keys that will be present in `public_spec` dict.
    PUBLIC_KEYS = {"stop", "num_samples", "time_budget_s"}

    def __init__(
        self,
        name: str,
        run: Union[str, Callable, Type],
        *,
        stop: Optional[Union[Mapping, Stopper, Callable[[str, Mapping], bool]]] = None,
        time_budget_s: Optional[Union[int, float, datetime.timedelta]] = None,
        config: Optional[Dict[str, Any]] = None,
        resources_per_trial: Union[
            None, Mapping[str, Union[float, int, Mapping]], "PlacementGroupFactory"
        ] = None,
        num_samples: int = 1,
        storage_path: Optional[str] = None,
        storage_filesystem: Optional["pyarrow.fs.FileSystem"] = None,
        _experiment_checkpoint_dir: Optional[str] = None,
        sync_config: Optional[Union[SyncConfig, dict]] = None,
        checkpoint_config: Optional[Union[CheckpointConfig, dict]] = None,
        trial_name_creator: Optional[Callable[["Trial"], str]] = None,
        trial_dirname_creator: Optional[Callable[["Trial"], str]] = None,
        log_to_file: bool = False,
        export_formats: Optional[Sequence] = None,
        max_failures: int = 0,
        restore: Optional[str] = None,
        # Deprecated
        local_dir: Optional[str] = None,
    ):
        if isinstance(checkpoint_config, dict):
            checkpoint_config = CheckpointConfig(**checkpoint_config)
        else:
            checkpoint_config = checkpoint_config or CheckpointConfig()

        if is_function_trainable(run):
            if checkpoint_config.checkpoint_at_end:
                raise ValueError(
                    "'checkpoint_at_end' cannot be used with a function trainable. "
                    "You should include one last call to "
                    "`ray.train.report(metrics=..., checkpoint=...)` "
                    "at the end of your training loop to get this behavior."
                )
            if checkpoint_config.checkpoint_frequency:
                raise ValueError(
                    "'checkpoint_frequency' cannot be set for a function trainable. "
                    "You will need to report a checkpoint every "
                    "`checkpoint_frequency` iterations within your training loop using "
                    "`ray.train.report(metrics=..., checkpoint=...)` "
                    "to get this behavior."
                )
        try:
            self._run_identifier = Experiment.register_if_needed(run)
        except RpcError as e:
            if e.rpc_code == grpc.StatusCode.RESOURCE_EXHAUSTED.value[0]:
                raise TuneError(
                    f"The Trainable/training function is too large for grpc resource "
                    f"limit. Check that its definition is not implicitly capturing a "
                    f"large array or other object in scope. "
                    f"Tip: use tune.with_parameters() to put large objects "
                    f"in the Ray object store. \n"
                    f"Original exception: {traceback.format_exc()}"
                )
            else:
                raise e

        self.storage = None
        if _use_storage_context():
            assert name is not None

            self.storage = StorageContext(
                storage_path=storage_path,
                storage_filesystem=storage_filesystem,
                sync_config=sync_config,
                experiment_dir_name=name,
            )
            logger.debug(f"StorageContext on the DRIVER:\n{self.storage}")

            self._legacy_local_storage_path = None
            self._legacy_remote_storage_path = None
            self.legacy_sync_config = None
            self.legacy_dir_name = None
            self.legacy_name = None
        else:
            if isinstance(sync_config, dict):
                sync_config = SyncConfig(**sync_config)
            else:
                sync_config = sync_config or SyncConfig()

            self.legacy_sync_config = sync_config

            # Resolve storage_path
            local_storage_path, remote_storage_path = _resolve_storage_path(
                storage_path,
                local_dir,
                sync_config.upload_dir,
                error_location="Experiment",
            )

            if local_dir:
                if log_once("tune_experiment_local_dir"):
                    warnings.warn(
                        "The `local_dir` argument of `Experiment is deprecated. "
                        "Use `storage_path` or set the `TUNE_RESULT_DIR` "
                        "environment variable instead."
                    )

                local_storage_path = local_dir

            full_local_storage_path = _get_local_dir_with_expand_user(
                local_storage_path
            )

            # `_experiment_checkpoint_dir` is for internal use only for better
            # support of Tuner API.
            # If set, it should be a subpath under `local_dir`. Also deduce `dir_name`.
            if _experiment_checkpoint_dir:
                experiment_checkpoint_dir_path = Path(_experiment_checkpoint_dir)
                local_dir_path = Path(full_local_storage_path)
                assert local_dir_path in experiment_checkpoint_dir_path.parents, (
                    local_dir_path,
                    str(list(experiment_checkpoint_dir_path.parents)),
                )
                # `dir_name` is set by `_experiment_checkpoint_dir` indirectly.
                self.legacy_dir_name = os.path.relpath(
                    _experiment_checkpoint_dir, full_local_storage_path
                )

            self._legacy_local_storage_path = full_local_storage_path
            self._legacy_remote_storage_path = remote_storage_path

            self.legacy_name = name or self._run_identifier

            if not _experiment_checkpoint_dir:
                self.legacy_dir_name = _get_dir_name(run, name, self.legacy_name)

            assert self.legacy_dir_name

        config = config or {}

        self._stopper = None
        stopping_criteria = {}
        if not stop:
            pass
        elif isinstance(stop, list):
            bad_stoppers = [s for s in stop if not isinstance(s, Stopper)]
            if bad_stoppers:
                stopper_types = [type(s) for s in stop]
                raise ValueError(
                    "If you pass a list as the `stop` argument to "
                    "`train.RunConfig()`, each element must be an instance of "
                    f"`tune.stopper.Stopper`. Got {stopper_types}."
                )
            self._stopper = CombinedStopper(*stop)
        elif isinstance(stop, dict):
            stopping_criteria = stop
        elif callable(stop):
            if FunctionStopper.is_valid_function(stop):
                self._stopper = FunctionStopper(stop)
            elif isinstance(stop, Stopper):
                self._stopper = stop
            else:
                raise ValueError(
                    "Provided stop object must be either a dict, "
                    "a function, or a subclass of "
                    f"`ray.tune.Stopper`. Got {type(stop)}."
                )
        else:
            raise ValueError(
                f"Invalid stop criteria: {stop}. Must be a "
                f"callable or dict. Got {type(stop)}."
            )

        if time_budget_s:
            if self._stopper:
                self._stopper = CombinedStopper(
                    self._stopper, TimeoutStopper(time_budget_s)
                )
            else:
                self._stopper = TimeoutStopper(time_budget_s)

        stdout_file, stderr_file = _validate_log_to_file(log_to_file)

        spec = {
            "run": self._run_identifier,
            "stop": stopping_criteria,
            "time_budget_s": time_budget_s,
            "config": config,
            "resources_per_trial": resources_per_trial,
            "num_samples": num_samples,
            "experiment_path": self.path,
            "experiment_dir_name": self.legacy_dir_name,
            "sync_config": self.legacy_sync_config,
            "checkpoint_config": checkpoint_config,
            "trial_name_creator": trial_name_creator,
            "trial_dirname_creator": trial_dirname_creator,
            "log_to_file": (stdout_file, stderr_file),
            "export_formats": export_formats or [],
            "max_failures": max_failures,
            "restore": os.path.abspath(os.path.expanduser(restore))
            if restore
            else None,
            "storage": self.storage,
        }
        self.spec = spec

    @classmethod
    def from_json(cls, name: str, spec: dict):
        """Generates an Experiment object from JSON.

        Args:
            name: Name of Experiment.
            spec: JSON configuration of experiment.
        """
        if "run" not in spec:
            raise TuneError("No trainable specified!")

        # Special case the `env` param for RLlib by automatically
        # moving it into the `config` section.
        if "env" in spec:
            spec["config"] = spec.get("config", {})
            spec["config"]["env"] = spec["env"]
            del spec["env"]

        if "sync_config" in spec and isinstance(spec["sync_config"], dict):
            spec["sync_config"] = SyncConfig(**spec["sync_config"])

        if "checkpoint_config" in spec and isinstance(spec["checkpoint_config"], dict):
            spec["checkpoint_config"] = CheckpointConfig(**spec["checkpoint_config"])

        spec = copy.deepcopy(spec)

        run_value = spec.pop("run")
        try:
            exp = cls(name, run_value, **spec)
        except TypeError as e:
            raise TuneError(
                f"Failed to load the following Tune experiment "
                f"specification:\n\n {pp.pformat(spec)}.\n\n"
                f"Please check that the arguments are valid. "
                f"Experiment creation failed with the following "
                f"error:\n {e}"
            )
        return exp

    @classmethod
    def get_trainable_name(cls, run_object: Union[str, Callable, Type]):
        """Get Trainable name.

        Args:
            run_object: Trainable to run. If string,
                assumes it is an ID and does not modify it. Otherwise,
                returns a string corresponding to the run_object name.

        Returns:
            A string representing the trainable identifier.

        Raises:
            TuneError: if ``run_object`` passed in is invalid.
        """
        from ray.tune.search.sample import Domain

        if isinstance(run_object, str) or isinstance(run_object, Domain):
            return run_object
        elif isinstance(run_object, type) or callable(run_object):
            name = "DEFAULT"
            if hasattr(run_object, "_name"):
                name = run_object._name
            elif hasattr(run_object, "__name__"):
                fn_name = run_object.__name__
                if fn_name == "<lambda>":
                    name = "lambda"
                elif fn_name.startswith("<"):
                    name = "DEFAULT"
                else:
                    name = fn_name
            elif (
                isinstance(run_object, partial)
                and hasattr(run_object, "func")
                and hasattr(run_object.func, "__name__")
            ):
                name = run_object.func.__name__
            else:
                logger.warning("No name detected on trainable. Using {}.".format(name))
            return name
        else:
            raise TuneError("Improper 'run' - not string nor trainable.")

    @classmethod
    def register_if_needed(cls, run_object: Union[str, Callable, Type]):
        """Registers Trainable or Function at runtime.

        Assumes already registered if run_object is a string.
        Also, does not inspect interface of given run_object.

        Args:
            run_object: Trainable to run. If string,
                assumes it is an ID and does not modify it. Otherwise,
                returns a string corresponding to the run_object name.

        Returns:
            A string representing the trainable identifier.
        """
        from ray.tune.search.sample import Domain

        if isinstance(run_object, str):
            return run_object
        elif isinstance(run_object, Domain):
            logger.warning("Not registering trainable. Resolving as variant.")
            return run_object
        name = cls.get_trainable_name(run_object)
        try:
            register_trainable(name, run_object)
        except (TypeError, PicklingError) as e:
            extra_msg = (
                "Other options: "
                "\n-Try reproducing the issue by calling "
                "`pickle.dumps(trainable)`. "
                "\n-If the error is typing-related, try removing "
                "the type annotations and try again."
            )
            raise type(e)(str(e) + " " + extra_msg) from None
        return name

    @classmethod
    def get_experiment_checkpoint_dir(
        cls,
        run_obj: Union[str, Callable, Type],
        storage_path: Optional[str] = None,
        name: Optional[str] = None,
    ):
        """Get experiment checkpoint dir without setting up an experiment.

        This is only used internally for better support of Tuner API.

        Args:
            run_obj: Trainable to run.
            storage_path: The path to Ray AIR's result storage.
            name: The name of the experiment specified by user.

        Returns:
            Checkpoint directory for experiment.
        """
        assert run_obj

        local_path, _ = _split_remote_local_path(storage_path, None)
        local_path = _get_local_dir_with_expand_user(local_path)

        run_identifier = cls.get_trainable_name(run_obj)
        combined_name = name or run_identifier

        dir_name = _get_dir_name(run_obj, name, combined_name)

        return os.path.join(local_path, dir_name)

    @property
    def stopper(self):
        return self._stopper

    @property
    def local_path(self) -> Optional[str]:
        if _use_storage_context():
            return self.storage.experiment_local_path

        if not self._legacy_local_storage_path:
            return None
        return str(Path(self._legacy_local_storage_path) / self.legacy_dir_name)

    @property
    @Deprecated("Replaced by `local_path`")
    def local_dir(self):
        # Deprecate: Raise in 2.5, Remove in 2.6
        return self.local_path

    @property
    def remote_path(self) -> Optional[str]:
        if _use_storage_context():
            return str(self.storage.storage_prefix / self.storage.experiment_fs_path)

        if not self._legacy_remote_storage_path:
            return None
        return str(URI(self._legacy_remote_storage_path) / self.legacy_dir_name)

    @property
    def path(self) -> Optional[str]:
        return self.remote_path or self.local_path

    @property
    def checkpoint_config(self):
        return self.spec.get("checkpoint_config")

    @property
    @Deprecated("Replaced by `checkpoint_dir`")
    def checkpoint_dir(self):
        # Deprecate: Raise in 2.5, Remove in 2.6
        # Provided when initializing Experiment, if so, return directly.
        return self.local_path

    @property
    @Deprecated("Replaced by `remote_path`")
    def remote_checkpoint_dir(self) -> Optional[str]:
        # Deprecate: Raise in 2.5, Remove in 2.6
        return self.remote_path

    @property
    def run_identifier(self):
        """Returns a string representing the trainable identifier."""
        return self._run_identifier

    @property
    def public_spec(self) -> Dict[str, Any]:
        """Returns the spec dict with only the public-facing keys.

        Intended to be used for passing information to callbacks,
        Searchers and Schedulers.
        """
        return {k: v for k, v in self.spec.items() if k in self.PUBLIC_KEYS}


def _convert_to_experiment_list(experiments: Union[Experiment, List[Experiment], Dict]):
    """Produces a list of Experiment objects.

    Converts input from dict, single experiment, or list of
    experiments to list of experiments. If input is None,
    will return an empty list.

    Arguments:
        experiments: Experiments to run.

    Returns:
        List of experiments.
    """
    exp_list = experiments

    # Transform list if necessary
    if experiments is None:
        exp_list = []
    elif isinstance(experiments, Experiment):
        exp_list = [experiments]
    elif type(experiments) is dict:
        exp_list = [
            Experiment.from_json(name, spec) for name, spec in experiments.items()
        ]

    # Validate exp_list
    if type(exp_list) is list and all(isinstance(exp, Experiment) for exp in exp_list):
        if len(exp_list) > 1:
            logger.info(
                "Running with multiple concurrent experiments. "
                "All experiments will be using the same SearchAlgorithm."
            )
    else:
        raise TuneError("Invalid argument: {}".format(experiments))

    return exp_list
