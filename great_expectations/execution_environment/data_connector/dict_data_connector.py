from pathlib import Path
import itertools
from typing import List, Union, Any

import logging

from great_expectations.execution_engine import ExecutionEngine
from great_expectations.execution_environment.data_connector.partitioner.partitioner import Partitioner
from great_expectations.execution_environment.data_connector.partitioner.partition_request import PartitionRequest
from great_expectations.execution_environment.data_connector.partitioner.partition import Partition
from great_expectations.execution_environment.data_connector.data_connector import DataConnector
from great_expectations.core.batch import BatchRequest
from great_expectations.core.id_dict import (
    PartitionDefinitionSubset,
    BatchSpec
)
from great_expectations.core.batch import (
    BatchMarkers,
    BatchDefinition,
)
from great_expectations.execution_environment.types import PathBatchSpec
import great_expectations.exceptions as ge_exceptions

logger = logging.getLogger(__name__)


class DictDataConnector(DataConnector):
    """This DataConnector is meant to closely mimic the FilesDataConnector, but without requiring an actual filesystem.

    Instead, its data_references are stored in a data_reference_dictionary : {
        "pretend/path/A-100.csv" : pandas_df_A_100,
        "pretend/path/A-101.csv" : pandas_df_A_101,
        "pretend/directory/B-1.csv" : pandas_df_B_1,
        "pretend/directory/B-2.csv" : pandas_df_B_2,
        ...
    }
    """
    def __init__(
        self,
        name: str,
        execution_environment_name: str,
        data_reference_dict: dict,
        partitioners: dict = None,
        default_partitioner_name: str = None,
        assets: dict = None,
        execution_engine: ExecutionEngine = None,
    ):
        if data_reference_dict is None:
            data_reference_dict = {}
        logger.debug(f'Constructing DictDataConnector "{name}".')
        super().__init__(
            name=name,
            execution_environment_name=execution_environment_name,
            partitioners=partitioners,
            default_partitioner_name=default_partitioner_name,
            assets=assets,
            execution_engine=execution_engine,
        )

        # This simulates the underlying filesystem
        self.data_reference_dict = data_reference_dict

        self._data_references_cache = None

    def _get_data_reference_list(self):
        data_reference_keys = list(self.data_reference_dict.keys())
        data_reference_keys.sort()
        return data_reference_keys
