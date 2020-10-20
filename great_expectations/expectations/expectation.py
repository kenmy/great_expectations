import logging
import re
import string
from abc import ABC, ABCMeta
from collections import Counter
from copy import deepcopy
from functools import wraps
from inspect import isabstract
from typing import Any, Callable, Dict, List, Optional, Type, Union

import pandas as pd
from dateutil.parser import parse

from great_expectations import __version__ as ge_version
from great_expectations.core.expectation_configuration import ExpectationConfiguration
from great_expectations.core.expectation_validation_result import (
    ExpectationValidationResult,
)
from great_expectations.exceptions import (
    GreatExpectationsError,
    InvalidExpectationConfigurationError,
    InvalidExpectationKwargsError,
)
from great_expectations.expectations.registry import (
    get_metric_kwargs,
    register_expectation, register_renderer,
)
from great_expectations.expectations.util import legacy_method_parameters

from ..core.batch import Batch
from ..core.util import nested_update
from ..data_asset.util import (
    parse_result_format,
    recursively_convert_to_json_serializable,
)
from ..exceptions.metric_exceptions import MetricError
from ..execution_engine import (
    ExecutionEngine,
    PandasExecutionEngine,
    SparkDFExecutionEngine,
)
from ..execution_engine.sqlalchemy_execution_engine import SqlAlchemyExecutionEngine
from ..validator.validation_graph import MetricConfiguration
from ..validator.validator import Validator

logger = logging.getLogger(__name__)


p1 = re.compile(r"(.)([A-Z][a-z]+)")
p2 = re.compile(r"([a-z0-9])([A-Z])")


def camel_to_snake(name):
    name = p1.sub(r"\1_\2", name)
    return p2.sub(r"\1_\2", name).lower()


class MetaExpectation(ABCMeta):
    """MetaExpectation registers Expectations as they are defined."""

    def __new__(cls, clsname, bases, attrs):
        newclass = super().__new__(cls, clsname, bases, attrs)
        if not isabstract(newclass):
            newclass.expectation_type = camel_to_snake(clsname)
            register_expectation(newclass)
        newclass._register_renderer_functions()
        default_kwarg_values = dict()
        for base in reversed(bases):
            default_kwargs = getattr(base, "default_kwarg_values", dict())
            default_kwarg_values = nested_update(default_kwarg_values, default_kwargs)

        newclass.default_kwarg_values = nested_update(
            default_kwarg_values, attrs.get("default_kwarg_values", dict())
        )
        return newclass


def renderer(renderer_name):
    def wrapper(renderer_fn):
        @wraps(renderer_fn)
        def inner_func(*args, **kwargs):
            return renderer_fn(*args, **kwargs)

        inner_func._renderer_name = renderer_name
        return inner_func

    return wrapper


class Expectation(ABC, metaclass=MetaExpectation):
    """Base class for all Expectations."""

    version = ge_version
    domain_keys = tuple()
    success_keys = tuple()
    runtime_keys = (
        "include_config",
        "catch_exceptions",
        "result_format",
    )
    default_kwarg_values = {
        "include_config": True,
        "catch_exceptions": False,
        "result_format": "BASIC",
    }
    legacy_method_parameters = legacy_method_parameters

    def __init__(self, configuration: Optional[ExpectationConfiguration] = None):
        if configuration is not None:
            self.validate_configuration(configuration)
        self._configuration = configuration

    @classmethod
    def _register_renderer_functions(cls):
        expectation_type = camel_to_snake(cls.__name__)

        for attr, candidate_renderer_fn in cls.__dict__.items():
            if not hasattr(candidate_renderer_fn, "_renderer_name"):
                continue
            renderer_fn = getattr(cls, attr)
            register_renderer(
                expectation_type=expectation_type,
                renderer_fn=renderer_fn
            )

    @renderer(renderer_name="descriptive")
    def _descriptive_renderer(self, ):
        pass

    @classmethod
    def get_allowed_config_keys(cls):
        return cls.domain_keys + cls.success_keys + cls.runtime_keys

    # TODO: revise signature; revise decorator
    def _validate(
        self,
        configuration: ExpectationConfiguration,
        metrics: Dict[str, Any],
        runtime_configuration: dict,
        execution_engine: ExecutionEngine,
    ):
        raise NotImplementedError

    def metrics_validate(
        self,
        metrics: dict,
        configuration: Optional[ExpectationConfiguration] = None,
        runtime_configuration: dict = None,
        execution_engine: ExecutionEngine = None,
    ) -> "ExpectationValidationResult":
        if configuration is None:
            configuration = self.configuration
        provided_metrics = dict()
        requested_metrics = self.get_validation_dependencies(
            configuration,
            execution_engine=execution_engine,
            runtime_configuration=runtime_configuration,
        )["metrics"]
        for name, metric_edge_key in requested_metrics.items():
            provided_metrics[name] = metrics[metric_edge_key.id]

        return self._build_evr(
            self._validate(
                configuration=configuration,
                metrics=provided_metrics,
                runtime_configuration=runtime_configuration,
                execution_engine=execution_engine,
            )
        )

    def _build_evr(self, raw_response):
        if not isinstance(raw_response, ExpectationValidationResult):
            if isinstance(raw_response, dict):
                return ExpectationValidationResult(**raw_response)
            else:
                raise GreatExpectationsError("Unable to build EVR")
        else:
            return raw_response

    def get_validation_dependencies(
        self,
        configuration: Optional[ExpectationConfiguration] = None,
        execution_engine: Optional[ExecutionEngine] = None,
        runtime_configuration: Optional[dict] = None,
    ):
        """Construct the validation graph for this expectation."""
        return {
            "result_format": parse_result_format(
                self.get_runtime_kwargs(
                    configuration=configuration,
                    runtime_configuration=runtime_configuration,
                ).get("result_format")
            ),
            "metrics": dict(),
        }

    def __check_validation_kwargs_definition(self):
        """As a convenience to implementers, we verify that validation kwargs are indeed always supersets of their
        parent validation_kwargs"""
        validation_kwargs_set = set(self.validation_kwargs)
        for parent in self.mro():
            assert validation_kwargs_set <= set(
                getattr(parent, "validation_kwargs", set())
            ), ("Invalid Expectation " "definition for : " + self.__class__.__name__)
        return True

    def get_domain_kwargs(
        self, configuration: Optional[ExpectationConfiguration] = None
    ):
        if not configuration:
            configuration = self.configuration

        domain_kwargs = {
            key: configuration.kwargs.get(key, self.default_kwarg_values.get(key))
            for key in self.domain_keys
        }
        missing_kwargs = set(self.domain_keys) - set(domain_kwargs.keys())
        if missing_kwargs:
            raise InvalidExpectationKwargsError(
                f"Missing domain kwargs: {list(missing_kwargs)}"
            )
        return domain_kwargs

    def get_success_kwargs(
        self, configuration: Optional[ExpectationConfiguration] = None
    ):
        if not configuration:
            configuration = self.configuration

        domain_kwargs = self.get_domain_kwargs(configuration)
        success_kwargs = {
            key: configuration.kwargs.get(key, self.default_kwarg_values.get(key))
            for key in self.success_keys
        }
        success_kwargs.update(domain_kwargs)
        return success_kwargs

    def get_runtime_kwargs(
        self,
        configuration: Optional[ExpectationConfiguration] = None,
        runtime_configuration: dict = None,
    ):
        if not configuration:
            configuration = self.configuration

        configuration = deepcopy(configuration)

        if runtime_configuration:
            configuration.kwargs.update(runtime_configuration)

        success_kwargs = self.get_success_kwargs(configuration)
        runtime_kwargs = {
            key: configuration.kwargs.get(key, self.default_kwarg_values.get(key))
            for key in self.runtime_keys
        }
        runtime_kwargs.update(success_kwargs)

        runtime_kwargs["result_format"] = parse_result_format(
            runtime_kwargs["result_format"]
        )

        return runtime_kwargs

    def validate_configuration(self, configuration: Optional[ExpectationConfiguration]):
        if configuration is None:
            configuration = self.configuration
        try:
            assert configuration.expectation_type == self.expectation_type, (
                "expectation configuration type does not match " "expectation type"
            )
        except AssertionError as e:
            raise InvalidExpectationConfigurationError(str(e))
        return True

    def validate(
        self,
        batches: Dict[str, Batch],
        execution_engine: ExecutionEngine,
        configuration: Optional[ExpectationConfiguration] = None,
        runtime_configuration=None,
    ):
        if configuration is None:
            configuration = self.configuration
        return Validator().graph_validate(
            batches=batches,
            execution_engine=execution_engine,
            configurations=[configuration],
            runtime_configuration=runtime_configuration,
        )[0]

    @property
    def configuration(self):
        if self._configuration is None:
            raise InvalidExpectationConfigurationError(
                "cannot access configuration: expectation has not yet been configured"
            )
        return self._configuration

    @classmethod
    def build_configuration(cls, *args, **kwargs):
        # Combine all arguments into a single new "all_args" dictionary to name positional parameters
        all_args = dict(zip(cls.validation_kwargs, args))
        all_args.update(kwargs)

        # Unpack display parameters; remove them from all_args if appropriate
        if "include_config" in kwargs:
            include_config = kwargs["include_config"]
            del all_args["include_config"]
        else:
            include_config = cls.default_expectation_args["include_config"]

        if "catch_exceptions" in kwargs:
            catch_exceptions = kwargs["catch_exceptions"]
            del all_args["catch_exceptions"]
        else:
            catch_exceptions = cls.default_expectation_args["catch_exceptions"]

        if "result_format" in kwargs:
            result_format = kwargs["result_format"]
        else:
            result_format = cls.default_expectation_args["result_format"]

        # Extract the meta object for use as a top-level expectation_config holder
        if "meta" in kwargs:
            meta = kwargs["meta"]
            del all_args["meta"]
        else:
            meta = None

        # all_args = recursively_convert_to_json_serializable(all_args)
        #
        # # Patch in PARAMETER args, and remove locally-supplied arguments
        # # This will become the stored config
        # expectation_args = copy.deepcopy(all_args)
        #
        # if self._expectation_suite.evaluation_parameters:
        #     evaluation_args = build_evaluation_parameters(
        #         expectation_args,
        #         self._expectation_suite.evaluation_parameters,
        #         self._config.get("interactive_evaluation", True)
        #     )
        # else:
        #     evaluation_args = build_evaluation_parameters(
        #         expectation_args, None, self._config.get("interactive_evaluation", True))

        # Construct the expectation_config object
        return ExpectationConfiguration(
            expectation_type=cls.expectation_type,
            kwargs=recursively_convert_to_json_serializable(deepcopy(all_args)),
            meta=meta,
        )

    def get_validator_name(self):
        """
        This is just a placeholder for more complex logic to determine the validator_name
        Returns:

        """
        return "default"


class DatasetExpectation(Expectation, ABC):
    domain_keys = (
        "batch_id",
        "table",
        "column",
        "row_condition",
        "condition_parser",
    )
    metric_dependencies = tuple()

    def get_validation_dependencies(
        self,
        configuration: Optional[ExpectationConfiguration] = None,
        execution_engine: Optional[ExecutionEngine] = None,
        runtime_configuration: Optional[dict] = None,
    ):
        dependencies = super().get_validation_dependencies(
            configuration, execution_engine, runtime_configuration
        )
        for metric_name in self.metric_dependencies:
            metric_kwargs = get_metric_kwargs(
                metric_name=metric_name,
                configuration=configuration,
                runtime_configuration=runtime_configuration,
            )
            dependencies["metrics"][metric_name] = MetricConfiguration(
                metric_name=metric_name,
                metric_domain_kwargs=metric_kwargs["metric_domain_kwargs"],
                metric_value_kwargs=metric_kwargs["metric_value_kwargs"],
            )

        return dependencies

    @staticmethod
    def get_value_set_parser(execution_engine: ExecutionEngine):
        if isinstance(execution_engine, PandasExecutionEngine):
            return DatasetExpectation._pandas_value_set_parser

        raise GreatExpectationsError(
            f"No parser found for backend: {str(execution_engine.__name__)}"
        )

    @staticmethod
    def _pandas_value_set_parser(value_set):
        parsed_value_set = [
            parse(value) if isinstance(value, str) else value for value in value_set
        ]
        return parsed_value_set

    def parse_value_set(
        self, execution_engine: Type[ExecutionEngine], value_set: Union[list, set]
    ):
        value_set_parser = self.get_value_set_parser(execution_engine)
        return value_set_parser(value_set)


class ColumnMapDatasetExpectation(DatasetExpectation, ABC):
    map_metric = None

    success_keys = ("mostly",)
    default_kwarg_values = {"mostly": 1}

    def validate_configuration(self, configuration: Optional[ExpectationConfiguration]):
        if not super().validate_configuration(configuration):
            return False
        try:
            assert (
                "column" in configuration.kwargs
            ), "'column' parameter is required for column map expectations"
            if "mostly" in configuration.kwargs:
                mostly = configuration.kwargs["mostly"]
                assert isinstance(
                    mostly, (int, float)
                ), "'mostly' parameter must be an integer or float"
                assert 0 <= mostly <= 1, "'mostly' parameter must be between 0 and 1"
        except AssertionError as e:
            raise InvalidExpectationConfigurationError(str(e))
        return True

    def get_validation_dependencies(
        self,
        configuration: Optional[ExpectationConfiguration] = None,
        execution_engine: Optional[ExecutionEngine] = None,
        runtime_configuration: Optional[dict] = None,
    ):
        dependencies = super().get_validation_dependencies(
            configuration, execution_engine, runtime_configuration
        )
        assert isinstance(
            self.map_metric, str
        ), "ColumnMapDatasetExpectation must override get_validation_dependencies or declare exactly one map_metric"

        # convenient name for updates
        metric_dependencies = dependencies["metrics"]
        metric_kwargs = get_metric_kwargs(
            metric_name="column_values.nonnull.unexpected_count",
            configuration=configuration,
            runtime_configuration=runtime_configuration,
        )
        metric_dependencies[
            "column_values.nonnull.unexpected_count"
        ] = MetricConfiguration(
            "column_values.nonnull.unexpected_count",
            metric_domain_kwargs=metric_kwargs["metric_domain_kwargs"],
            metric_value_kwargs=metric_kwargs["metric_value_kwargs"],
        )
        metric_kwargs = get_metric_kwargs(
            metric_name=self.map_metric + ".unexpected_count",
            configuration=configuration,
            runtime_configuration=runtime_configuration,
        )
        metric_dependencies[
            self.map_metric + ".unexpected_count"
        ] = MetricConfiguration(
            self.map_metric + ".unexpected_count",
            metric_domain_kwargs=metric_kwargs["metric_domain_kwargs"],
            metric_value_kwargs=metric_kwargs["metric_value_kwargs"],
        )

        result_format_str = dependencies["result_format"].get("result_format")
        if result_format_str == "BOOLEAN_ONLY":
            return dependencies

        metric_kwargs = get_metric_kwargs(
            metric_name="table.row_count",
            configuration=configuration,
            runtime_configuration=runtime_configuration,
        )
        metric_dependencies["table.row_count"] = MetricConfiguration(
            metric_name="table.row_count",
            metric_domain_kwargs=metric_kwargs["metric_domain_kwargs"],
            metric_value_kwargs=metric_kwargs["metric_value_kwargs"],
        )

        metric_kwargs = get_metric_kwargs(
            self.map_metric + ".unexpected_values",
            configuration=configuration,
            runtime_configuration=runtime_configuration,
        )
        metric_dependencies[
            self.map_metric + ".unexpected_values"
        ] = MetricConfiguration(
            metric_name=self.map_metric + ".unexpected_values",
            metric_domain_kwargs=metric_kwargs["metric_domain_kwargs"],
            metric_value_kwargs=metric_kwargs["metric_value_kwargs"],
        )
        # TODO:
        #
        # if ".unexpected_index_list" is a registered metric **for this engine**
        if result_format_str in ["BASIC", "SUMMARY"]:
            return dependencies

        metric_kwargs = get_metric_kwargs(
            self.map_metric + ".unexpected_rows",
            configuration=configuration,
            runtime_configuration=runtime_configuration,
        )
        metric_dependencies[self.map_metric + ".unexpected_rows"] = MetricConfiguration(
            metric_name=self.map_metric + ".unexpected_rows",
            metric_domain_kwargs=metric_kwargs["metric_domain_kwargs"],
            metric_value_kwargs=metric_kwargs["metric_value_kwargs"],
        )
        if isinstance(execution_engine, PandasExecutionEngine):
            metric_kwargs = get_metric_kwargs(
                self.map_metric + ".unexpected_index_list",
                configuration=configuration,
                runtime_configuration=runtime_configuration,
            )
            metric_dependencies[
                self.map_metric + ".unexpected_index_list"
            ] = MetricConfiguration(
                metric_name=self.map_metric + ".unexpected_index_list",
                metric_domain_kwargs=metric_kwargs["metric_domain_kwargs"],
                metric_value_kwargs=metric_kwargs["metric_value_kwargs"],
            )

        return dependencies

    def _validate(
        self,
        configuration: ExpectationConfiguration,
        metrics: dict,
        runtime_configuration: dict = None,
        execution_engine: ExecutionEngine = None,
    ):

        if runtime_configuration:
            result_format = runtime_configuration.get(
                "result_format",
                configuration.kwargs.get(
                    "result_format", self.default_kwarg_values.get("result_format")
                ),
            )
        else:
            result_format = configuration.kwargs.get(
                "result_format", self.default_kwarg_values.get("result_format")
            )
        mostly = self.get_success_kwargs().get(
            "mostly", self.default_kwarg_values.get("mostly")
        )
        null_count = metrics["column_values.nonnull.unexpected_count"]
        if null_count is not None and null_count > 0:
            success = (
                1
                - (
                    metrics[self.map_metric + ".unexpected_count"]
                    / metrics["column_values.nonnull.unexpected_count"]
                )
            ) >= mostly
        else:
            success = None

        return _format_map_output(
            result_format=parse_result_format(result_format),
            success=success,
            element_count=metrics.get("table.row_count"),
            nonnull_count=metrics.get("table.row_count")
            - metrics.get("column_values.nonnull.unexpected_count"),
            unexpected_count=metrics.get(self.map_metric + ".unexpected_count"),
            unexpected_list=metrics.get(self.map_metric + ".unexpected_values"),
            unexpected_index_list=metrics.get(
                self.map_metric + ".unexpected_index_list"
            ),
        )


def _calc_map_expectation_success(success_count, nonnull_count, mostly):
    """Calculate success and percent_success for column_map_expectations

    Args:
        success_count (int): \
            The number of successful values in the column
        nonnull_count (int): \
            The number of nonnull values in the column
        mostly (float or None): \
            A value between 0 and 1 (or None), indicating the fraction of successes required to pass the \
            expectation as a whole. If mostly=None, then all values must succeed in order for the expectation as \
            a whole to succeed.

    Returns:
        success (boolean), percent_success (float)
    """

    if nonnull_count > 0:
        # percent_success = float(success_count)/nonnull_count
        percent_success = success_count / nonnull_count

        if mostly is not None:
            success = bool(percent_success >= mostly)

        else:
            success = bool(nonnull_count - success_count == 0)

    else:
        success = True
        percent_success = None

    return success, percent_success


def _format_map_output(
    result_format,
    success,
    element_count,
    nonnull_count,
    unexpected_count,
    unexpected_list,
    unexpected_index_list,
):
    """Helper function to construct expectation result objects for map_expectations (such as column_map_expectation
    and file_lines_map_expectation).

    Expectations support four result_formats: BOOLEAN_ONLY, BASIC, SUMMARY, and COMPLETE.
    In each case, the object returned has a different set of populated fields.
    See :ref:`result_format` for more information.

    This function handles the logic for mapping those fields for column_map_expectations.
    """
    # NB: unexpected_count parameter is explicit some implementing classes may limit the length of unexpected_list
    # Incrementally add to result and return when all values for the specified level are present
    return_obj = {"success": success}

    if result_format["result_format"] == "BOOLEAN_ONLY":
        return return_obj

    skip_missing = False

    if nonnull_count is None:
        missing_count = None
        skip_missing: bool = True
    else:
        missing_count = element_count - nonnull_count

    if element_count > 0:
        unexpected_percent = unexpected_count / element_count * 100

        if not skip_missing:
            missing_percent = missing_count / element_count * 100
            if nonnull_count > 0:
                unexpected_percent_nonmissing = unexpected_count / nonnull_count * 100
            else:
                unexpected_percent_nonmissing = None

    else:
        missing_percent = None
        unexpected_percent = None
        unexpected_percent_nonmissing = None

    return_obj["result"] = {
        "element_count": element_count,
        "unexpected_count": unexpected_count,
        "unexpected_percent": unexpected_percent,
        "partial_unexpected_list": unexpected_list[
            : result_format["partial_unexpected_count"]
        ],
    }

    if not skip_missing:
        return_obj["result"]["missing_count"] = missing_count
        return_obj["result"]["missing_percent"] = missing_percent
        return_obj["result"][
            "unexpected_percent_nonmissing"
        ] = unexpected_percent_nonmissing

    if result_format["result_format"] == "BASIC":
        return return_obj

    # Try to return the most common values, if possible.
    if 0 < result_format.get("partial_unexpected_count"):
        try:
            partial_unexpected_counts = [
                {"value": key, "count": value}
                for key, value in sorted(
                    Counter(unexpected_list).most_common(
                        result_format["partial_unexpected_count"]
                    ),
                    key=lambda x: (-x[1], x[0]),
                )
            ]
        except TypeError:
            partial_unexpected_counts = [
                "partial_exception_counts requires a hashable type"
            ]
        finally:
            return_obj["result"].update(
                {
                    "partial_unexpected_index_list": unexpected_index_list[
                        : result_format["partial_unexpected_count"]
                    ]
                    if unexpected_index_list is not None
                    else None,
                    "partial_unexpected_counts": partial_unexpected_counts,
                }
            )

    if result_format["result_format"] == "SUMMARY":
        return return_obj

    return_obj["result"].update(
        {
            "unexpected_list": unexpected_list,
            "unexpected_index_list": unexpected_index_list,
        }
    )

    if result_format["result_format"] == "COMPLETE":
        return return_obj

    raise ValueError("Unknown result_format {}.".format(result_format["result_format"]))
