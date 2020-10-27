from typing import Optional

import pandas as pd

from great_expectations.core.expectation_configuration import ExpectationConfiguration
from great_expectations.execution_engine import ExecutionEngine, PandasExecutionEngine

from ...data_asset.util import parse_result_format
from ...exceptions import InvalidExpectationConfigurationError
from ...execution_engine.sqlalchemy_execution_engine import SqlAlchemyExecutionEngine
from ...render.renderer.renderer import renderer
from ...render.types import RenderedStringTemplateContent
from ...render.util import (
    handle_strict_min_max,
    num_to_str,
    parse_row_condition_string_pandas_engine,
    substitute_none_for_missing,
)
from ..expectation import ColumnMapDatasetExpectation, Expectation, _format_map_output
from ..registry import extract_metrics

try:
    import sqlalchemy as sa
except ImportError:
    pass


class ExpectColumnValueLengthsToBeBetween(ColumnMapDatasetExpectation):
    """Expect column entries to be strings with length between a minimum value and a maximum value (inclusive).

    This expectation only works for string-type values. Invoking it on ints or floats will raise a TypeError.

    expect_column_value_lengths_to_be_between is a \
    :func:`column_map_expectation <great_expectations.execution_engine.execution_engine.MetaExecutionEngine
    .column_map_expectation>`.

    Args:
        column (str): \
            The column name.

    Keyword Args:
        min_value (int or None): \
            The minimum value for a column entry length.
        max_value (int or None): \
            The maximum value for a column entry length.
        mostly (None or a float between 0 and 1): \
            Return `"success": True` if at least mostly fraction of values match the expectation. \
            For more detail, see :ref:`mostly`.

    Other Parameters:
        result_format (str or None): \
            Which output mode to use: `BOOLEAN_ONLY`, `BASIC`, `COMPLETE`, or `SUMMARY`.
            For more detail, see :ref:`result_format <result_format>`.
        include_config (boolean): \
            If True, then include the expectation config as part of the result object. \
            For more detail, see :ref:`include_config`.
        catch_exceptions (boolean or None): \
            If True, then catch exceptions and include them as part of the result object. \
            For more detail, see :ref:`catch_exceptions`.
        meta (dict or None): \
            A JSON-serializable dictionary (nesting allowed) that will be included in the output without \
            modification. For more detail, see :ref:`meta`.

    Returns:
        An ExpectationSuiteValidationResult

        Exact fields vary depending on the values passed to :ref:`result_format <result_format>` and
        :ref:`include_config`, :ref:`catch_exceptions`, and :ref:`meta`.

    Notes:
        * min_value and max_value are both inclusive.
        * If min_value is None, then max_value is treated as an upper bound, and the number of acceptable rows has \
          no minimum.
        * If max_value is None, then min_value is treated as a lower bound, and the number of acceptable rows has \
          no maximum.

    See Also:
        :func:`expect_column_value_lengths_to_equal \
        <great_expectations.execution_engine.execution_engine.ExecutionEngine.expect_column_value_lengths_to_equal>`

    """

    map_metric = "column_values.value_length_between"
    success_keys = (
        "min_value",
        "max_value",
        "strict_min",
        "strict_max",
        "mostly",
    )

    default_kwarg_values = {
        "row_condition": None,
        "condition_parser": None,
        "min_value": None,
        "max_value": None,
        "strict_min": None,
        "strict_max": None,
        "mostly": 1,
        "result_format": "BASIC",
        "include_config": True,
        "catch_exceptions": False,
    }

    def validate_configuration(self, configuration: Optional[ExpectationConfiguration]):
        super().validate_configuration(configuration)

        if configuration is None:
            configuration = self.configuration

        try:
            assert configuration.kwargs.get(
                "min_value"
            ) is not None or configuration.kwargs.get(
                "max_value" != None
            ), "min_value and max_value cannot both be None"
            if configuration.kwargs.get("min_value"):
                assert float(
                    configuration.kwargs.get("min_value")
                ).is_integer(), "min_value and max_value must be integers"
            if configuration.kwargs.get("max_value"):
                assert float(
                    configuration.kwargs.get("max_value")
                ).is_integer(), "min_value and max_value must be integers"
        except AssertionError as e:
            raise InvalidExpectationConfigurationError(str(e))
        return True

    @classmethod
    @renderer(renderer_type="renderer.prescriptive")
    def _prescriptive_renderer(
        cls,
        configuration=None,
        result=None,
        language=None,
        runtime_configuration=None,
        **kwargs,
    ):
        runtime_configuration = runtime_configuration or {}
        include_column_name = runtime_configuration.get("include_column_name", True)
        styling = runtime_configuration.get("styling")
        params = substitute_none_for_missing(
            configuration.kwargs,
            [
                "column",
                "min_value",
                "max_value",
                "mostly",
                "row_condition",
                "condition_parser",
                "strict_min",
                "strict_max",
            ],
        )

        if (params["min_value"] is None) and (params["max_value"] is None):
            template_str = "values may have any length."
        else:
            at_least_str, at_most_str = handle_strict_min_max(params)

            if params["mostly"] is not None:
                params["mostly_pct"] = num_to_str(
                    params["mostly"] * 100, precision=15, no_scientific=True
                )
                # params["mostly_pct"] = "{:.14f}".format(params["mostly"]*100).rstrip("0").rstrip(".")
                if params["min_value"] is not None and params["max_value"] is not None:
                    template_str = f"values must be {at_least_str} $min_value and {at_most_str} $max_value characters long, at least $mostly_pct % of the time."

                elif params["min_value"] is None:
                    template_str = f"values must be {at_most_str} $max_value characters long, at least $mostly_pct % of the time."

                elif params["max_value"] is None:
                    template_str = f"values must be {at_least_str} $min_value characters long, at least $mostly_pct % of the time."
            else:
                if params["min_value"] is not None and params["max_value"] is not None:
                    template_str = f"values must always be {at_least_str} $min_value and {at_most_str} $max_value characters long."

                elif params["min_value"] is None:
                    template_str = f"values must always be {at_most_str} $max_value characters long."

                elif params["max_value"] is None:
                    template_str = f"values must always be {at_least_str} $min_value characters long."

        if include_column_name:
            template_str = "$column " + template_str

        if params["row_condition"] is not None:
            (
                conditional_template_str,
                conditional_params,
            ) = parse_row_condition_string_pandas_engine(params["row_condition"])
            template_str = conditional_template_str + ", then " + template_str
            params.update(conditional_params)

        return [
            RenderedStringTemplateContent(
                **{
                    "content_block_type": "string_template",
                    "string_template": {
                        "template": template_str,
                        "params": params,
                        "styling": styling,
                    },
                }
            )
        ]

    # @PandasExecutionEngine.column_map_metric(
    #     metric_name="column_values.value_length_between",
    #     metric_domain_keys=ColumnMapDatasetExpectation.domain_keys,
    #     metric_value_keys=("min_value", "max_value", "strict_min", "strict_max"),
    #     metric_dependencies=tuple(),
    #     filter_column_isnull=True,
    # )
    def _pandas_value_length_between(
        self,
        series: pd.Series,
        metrics: dict,
        metric_domain_kwargs: dict,
        metric_value_kwargs: dict,
        runtime_configuration: dict = None,
        filter_column_isnull: bool = True,
    ):
        min_value = metric_value_kwargs["min_value"]
        max_value = metric_value_kwargs["max_value"]
        strict_min = metric_value_kwargs["strict_min"]
        strict_max = metric_value_kwargs["strict_max"]

        column_lengths = series.astype(str).str.len()

        if min_value is not None and max_value is not None:
            if strict_min and strict_max:
                metric_series = column_lengths.between(
                    min_value, max_value, inclusive=False
                )
            elif strict_min and not strict_max:
                metric_series = (column_lengths > min_value) & (
                    column_lengths <= max_value
                )
            elif not strict_min and strict_max:
                metric_series = (column_lengths >= min_value) & (
                    column_lengths < max_value
                )
            elif not strict_min and not strict_max:
                metric_series = column_lengths.between(
                    min_value, max_value, inclusive=True
                )
        elif min_value is None and max_value is not None:
            if strict_max:
                metric_series = column_lengths < max_value
            else:
                metric_series = column_lengths <= max_value
        elif min_value is not None and max_value is None:
            if strict_min:
                metric_series = column_lengths > min_value
            else:
                metric_series = column_lengths >= min_value

        return pd.DataFrame({"column_values.value_length_between": metric_series})

    # @SqlAlchemyExecutionEngine.column_map_metric(
    #     metric_name="column_values.value_length_between",
    #     metric_domain_keys=ColumnMapDatasetExpectation.domain_keys,
    #     metric_value_keys=("min_value", "max_value", "strict_min", "strict_max"),
    #     metric_dependencies=tuple(),
    #     filter_column_isnull=True,
    # )
    def _sqlalchemy_value_length_between(
        self,
        column: sa.column,
        metrics: dict,
        metric_domain_kwargs: dict,
        metric_value_kwargs: dict,
        runtime_configuration: dict = None,
        filter_column_isnull: bool = True,
    ):
        min_value = metric_value_kwargs["min_value"]
        max_value = metric_value_kwargs["max_value"]
        strict_min = metric_value_kwargs["strict_min"]
        strict_max = metric_value_kwargs["strict_max"]

        if min_value is not None and max_value is not None:
            if strict_min and strict_max:
                return sa.and_(
                    sa.func.length(column) > min_value,
                    sa.func.length(column) < max_value,
                )
            elif strict_min and not strict_max:
                return sa.and_(
                    sa.func.length(column) > min_value,
                    sa.func.length(column) <= max_value,
                )
            elif not strict_min and strict_max:
                return sa.and_(
                    sa.func.length(column) >= min_value,
                    sa.func.length(column) < max_value,
                )
            elif not strict_min and not strict_max:
                return sa.and_(
                    sa.func.length(column) >= min_value,
                    sa.func.length(column) <= max_value,
                )
        elif min_value is None and max_value is not None:
            if strict_max:
                return sa.func.length(column) < max_value
            else:
                return sa.func.length(column) <= max_value
        elif min_value is not None and max_value is None:
            if strict_min:
                return sa.func.length(column) > min_value
            else:
                return sa.func.length(column) >= min_value
