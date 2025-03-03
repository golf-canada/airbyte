"""
MIT License

Copyright (c) 2020 Airbyte

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Tuple

import pytest
from airbyte_protocol import ConfiguredAirbyteCatalog, Type
from source_acceptance_test import BaseTest
from source_acceptance_test.utils import ConnectorRunner, JsonSchemaHelper, filter_output, incremental_only_catalog


@pytest.fixture(name="future_state_path")
def future_state_path_fixture(inputs, base_path) -> Path:
    """Fixture with connector's future state path (relative to base_path)"""
    if getattr(inputs, "state_path"):
        return Path(base_path) / getattr(inputs, "state_path")
    pytest.skip("`state_path` not specified, skipping")


@pytest.fixture(name="future_state")
def future_state_fixture(future_state_path) -> Path:
    """"""
    with open(str(future_state_path), "r") as file:
        contents = file.read()
    return json.loads(contents)


@pytest.fixture(name="cursor_paths")
def cursor_paths_fixture(inputs, configured_catalog_for_incremental) -> Mapping[str, Any]:
    cursor_paths = getattr(inputs, "cursor_paths")
    result = {}

    for stream in configured_catalog_for_incremental.streams:
        path = cursor_paths.get(stream.stream.name, [stream.cursor_field[-1]])
        result[stream.stream.name] = path

    return result


@pytest.fixture(name="configured_catalog_for_incremental")
def configured_catalog_for_incremental_fixture(configured_catalog) -> ConfiguredAirbyteCatalog:
    catalog = incremental_only_catalog(configured_catalog)
    for stream in catalog.streams:
        if not stream.cursor_field:
            pytest.fail("Configured catalog should have cursor_field specified for all incremental streams")
    return catalog


def records_with_state(records, state, stream_mapping, state_cursor_paths) -> Iterable[Tuple[Any, Any]]:
    """Iterate over records and return cursor value with corresponding cursor value from state"""
    for record in records:
        stream_name = record.record.stream
        stream = stream_mapping[stream_name]
        helper = JsonSchemaHelper(schema=stream.stream.json_schema)
        record_value = helper.get_cursor_value(record=record.record.data, cursor_path=stream.cursor_field)
        state_value = helper.get_state_value(state=state[stream_name], cursor_path=state_cursor_paths[stream_name])
        yield record_value, state_value


@pytest.mark.timeout(20 * 60)
class TestIncremental(BaseTest):
    def test_two_sequential_reads(self, connector_config, configured_catalog_for_incremental, cursor_paths, docker_runner: ConnectorRunner):
        stream_mapping = {stream.stream.name: stream for stream in configured_catalog_for_incremental.streams}

        output = docker_runner.call_read(connector_config, configured_catalog_for_incremental)
        records_1 = filter_output(output, type_=Type.RECORD)
        states_1 = filter_output(output, type_=Type.STATE)

        assert states_1, "Should produce at least one state"
        assert records_1, "Should produce at least one record"

        latest_state = states_1[-1].state.data
        for record_value, state_value in records_with_state(records_1, latest_state, stream_mapping, cursor_paths):
            assert (
                record_value <= state_value
            ), "First incremental sync should produce records younger or equal to cursor value from the state"

        output = docker_runner.call_read_with_state(connector_config, configured_catalog_for_incremental, state=latest_state)
        records_2 = filter_output(output, type_=Type.RECORD)

        for record_value, state_value in records_with_state(records_2, latest_state, stream_mapping, cursor_paths):
            assert (
                record_value >= state_value
            ), "Second incremental sync should produce records older or equal to cursor value from the state"

    def test_state_with_abnormally_large_values(self, connector_config, configured_catalog, future_state, docker_runner: ConnectorRunner):
        configured_catalog = incremental_only_catalog(configured_catalog)
        output = docker_runner.call_read_with_state(config=connector_config, catalog=configured_catalog, state=future_state)
        records = filter_output(output, type_=Type.RECORD)
        states = filter_output(output, type_=Type.STATE)

        assert not records, "The sync should produce no records when run with the state with abnormally large values"
        assert states, "The sync should produce at least one STATE message"
