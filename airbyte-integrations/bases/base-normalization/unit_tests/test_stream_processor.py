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


import os
import re
from typing import List

import pytest
from airbyte_protocol.models.airbyte_protocol import DestinationSyncMode, SyncMode
from normalization.destination_type import DestinationType
from normalization.transform_catalog.catalog_processor import CatalogProcessor, add_table_to_registry, read_json
from normalization.transform_catalog.destination_name_transformer import DestinationNameTransformer
from normalization.transform_catalog.stream_processor import StreamProcessor, get_table_name


@pytest.fixture
def setup_test_path():
    # This makes the test pass no matter if it is executed from Tests folder (with pytest) or from base-normalization folder (through pycharm)
    if os.path.exists(os.path.join(os.curdir, "unit_tests")):
        os.chdir("unit_tests")


@pytest.mark.parametrize(
    "catalog_file",
    [
        "edge_cases_catalog",
        "hubspot_catalog",
        "facebook_catalog",
        "stripe_catalog",
    ],
)
@pytest.mark.parametrize(
    "integration_type",
    [
        "Postgres",
        "BigQuery",
        "Snowflake",
        "Redshift",
    ],
)
def test_stream_processor_tables_naming(integration_type: str, catalog_file: str, setup_test_path):
    destination_type = DestinationType.from_string(integration_type)
    tables_registry = {}

    substreams = []
    catalog = read_json(f"resources/{catalog_file}.json")

    # process top level
    for stream_processor in CatalogProcessor.build_stream_processor(
        catalog=catalog,
        json_column_name="'json_column_name_test'",
        default_schema="schema_test",
        name_transformer=DestinationNameTransformer(destination_type),
        destination_type=destination_type,
        tables_registry=tables_registry,
    ):
        nested_processors = stream_processor.process()
        for schema in stream_processor.local_registry:
            for table in stream_processor.local_registry[schema]:
                found_sql_output = False
                for sql_output in stream_processor.sql_outputs:
                    file_name = f"{schema}_{table}"
                    if len(file_name) > stream_processor.name_transformer.get_name_max_length():
                        file_name = stream_processor.name_transformer.truncate_identifier_name(input_name=file_name)

                    if re.match(r".*/" + file_name + ".sql", sql_output) is not None:
                        found_sql_output = True
                        break
                assert found_sql_output
        add_table_to_registry(tables_registry, stream_processor)
        if nested_processors and len(nested_processors) > 0:
            substreams += nested_processors

    if os.path.exists(f"resources/{catalog_file}_expected_top_level_{integration_type.lower()}.json"):
        expected_top_level = set(read_json(f"resources/{catalog_file}_expected_top_level_{integration_type.lower()}.json")["tables"])
    else:
        expected_top_level = set(read_json(f"resources/{catalog_file}_expected_top_level.json")["tables"])
        if DestinationType.SNOWFLAKE.value == destination_type.value:
            expected_top_level = {table.upper() for table in expected_top_level}
        elif DestinationType.REDSHIFT.value == destination_type.value:
            expected_top_level = {table.lower() for table in expected_top_level}

    # process substreams
    while substreams:
        children = substreams
        substreams = []
        for substream in children:
            substream.tables_registry = tables_registry
            nested_processors = substream.process()
            add_table_to_registry(tables_registry, substream)
            if nested_processors:
                substreams += nested_processors

    if os.path.exists(f"resources/{catalog_file}_expected_nested_{integration_type.lower()}.json"):
        expected_nested = set(read_json(f"resources/{catalog_file}_expected_nested_{integration_type.lower()}.json")["tables"])
    else:
        expected_nested = set(read_json(f"resources/{catalog_file}_expected_nested.json")["tables"])
        if DestinationType.SNOWFLAKE.value == destination_type.value:
            expected_nested = {table.upper() for table in expected_nested}
        elif DestinationType.REDSHIFT.value == destination_type.value:
            expected_nested = {table.lower() for table in expected_nested}

    # TODO(davin): Instead of unwrapping all tables, rewrite this test so tables are compared based on schema.
    all_tables = set()
    for schema in tables_registry:
        for tables in tables_registry[schema]:
            all_tables.add(tables)

    assert (all_tables - expected_top_level) == expected_nested


@pytest.mark.parametrize(
    "root_table, base_table_name, suffix, expected",
    [
        (
            "abcdefghijklmnopqrstuvwxyz",
            "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz",
            "",
            "abcdefghij_c86_abcdefghijklm__nopqrstuvwxyz",
        ),
        (
            "abcdefghijklmnopqrstuvwxyz",
            "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz",
            "_ab1",
            "abcdefghij_c86_abcdefghijk__pqrstuvwxyz_ab1",
        ),
        ("abcde", "fghijk", "_ab1", "abcde_c86_fghijk_ab1"),
        ("abcde", "fghijk", "ab1", "abcde_c86_fghijk_ab1"),
        ("abcde", "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz", "", "abcde_c86_abcdefghijklmnop__lmnopqrstuvwxyz"),
        ("", "abcdefghijklmnopqrstuvwxyzabcdefghijklmnopqrstuvwxyz", "", "abcdefghijklmnopqrst__fghijklmnopqrstuvwxyz"),
    ],
)
def test_get_table_name(root_table: str, base_table_name: str, suffix: str, expected: str):
    name_transformer = DestinationNameTransformer(DestinationType.POSTGRES)
    name = get_table_name(name_transformer, root_table, base_table_name, suffix, ["json", "path"])
    assert name == expected
    assert len(name) <= 43  # explicitly check for our max postgres length in case tests are changed in the future


@pytest.mark.parametrize(
    "stream_name, is_intermediate, suffix, expected, expected_final_name",
    [
        ("stream_name", False, "", "stream_name", "stream_name"),
        ("stream_name", False, "suffix", "stream_name_suffix", "stream_name_suffix"),
        ("stream_name", False, "_suffix", "stream_name_suffix", "stream_name_suffix"),
        ("stream_name", True, "suffix", "stream_name_suffix", ""),
        ("stream_name", True, "_suffix", "stream_name_suffix", ""),
    ],
)
def test_generate_new_table_name(stream_name: str, is_intermediate: bool, suffix: str, expected: str, expected_final_name: str):
    stream_processor = StreamProcessor.create(
        stream_name=stream_name,
        destination_type=DestinationType.POSTGRES,
        raw_schema="raw_schema",
        schema="schema_name",
        source_sync_mode=SyncMode.full_refresh,
        destination_sync_mode=DestinationSyncMode.append_dedup,
        cursor_field=[],
        primary_key=[],
        json_column_name="json_column_name",
        properties=[],
        tables_registry=dict(),
        from_table="",
    )
    assert stream_processor.generate_new_table_name(is_intermediate=is_intermediate, suffix=suffix) == expected
    assert stream_processor.final_table_name == expected_final_name


@pytest.mark.parametrize(
    "stream_name, is_intermediate, suffix, expected, expected_final_name",
    [
        ("stream_name", False, "", "stream_name_485", "stream_name_485"),
        ("stream_name", False, "suffix", "stream_name_suffix_485", "stream_name_suffix_485"),
        ("stream_name", False, "_suffix", "stream_name_suffix_485", "stream_name_suffix_485"),
        ("stream_name", True, "suffix", "stream_name_suffix_485", ""),
        ("stream_name", True, "_suffix", "stream_name_suffix_485", ""),
    ],
)
def test_collisions_generate_new_table_name(stream_name: str, is_intermediate: bool, suffix: str, expected: str, expected_final_name: str):
    # fill test_registry with the same stream names as if it was already used so there would be collisions...
    test_registry = dict()
    test_registry["schema_name"] = set()
    test_registry["schema_name"].add("stream_name")
    test_registry["schema_name"].add("stream_name_suffix")
    test_registry["raw_schema"] = set()
    test_registry["raw_schema"].add("stream_name_suffix")
    stream_processor = StreamProcessor.create(
        stream_name=stream_name,
        destination_type=DestinationType.POSTGRES,
        raw_schema="raw_schema",
        schema="schema_name",
        source_sync_mode=SyncMode.full_refresh,
        destination_sync_mode=DestinationSyncMode.append_dedup,
        cursor_field=[],
        primary_key=[],
        json_column_name="json_column_name",
        properties=[],
        tables_registry=test_registry,
        from_table="",
    )
    assert stream_processor.generate_new_table_name(is_intermediate=is_intermediate, suffix=suffix) == expected
    assert stream_processor.final_table_name == expected_final_name


@pytest.mark.parametrize(
    "stream_name, is_intermediate, suffix, expected, expected_final_name",
    [
        ("stream_name", False, "", "stream_name_b00_child_stream", "stream_name_b00_child_stream"),
        ("stream_name", False, "suffix", "stream_name_b00_child_stream_suffix", "stream_name_b00_child_stream_suffix"),
        ("stream_name", False, "_suffix", "stream_name_b00_child_stream_suffix", "stream_name_b00_child_stream_suffix"),
        ("stream_name", True, "suffix", "stream_name_b00_child_stream_suffix", ""),
        ("stream_name", True, "_suffix", "stream_name_b00_child_stream_suffix", ""),
    ],
)
def test_nested_generate_new_table_name(stream_name: str, is_intermediate: bool, suffix: str, expected: str, expected_final_name: str):
    stream_processor = StreamProcessor.create(
        stream_name=stream_name,
        destination_type=DestinationType.POSTGRES,
        raw_schema="raw_schema",
        schema="schema_name",
        source_sync_mode=SyncMode.full_refresh,
        destination_sync_mode=DestinationSyncMode.append_dedup,
        cursor_field=[],
        primary_key=[],
        json_column_name="json_column_name",
        properties=[],
        tables_registry=dict(),
        from_table="",
    )
    nested_stream_processor = StreamProcessor.create_from_parent(
        parent=stream_processor,
        child_name="child_stream",
        json_column_name="json_column_name",
        properties=[],
        is_nested_array=False,
        from_table="",
    )
    assert nested_stream_processor.generate_new_table_name(is_intermediate=is_intermediate, suffix=suffix) == expected
    assert nested_stream_processor.final_table_name == expected_final_name


@pytest.mark.parametrize(
    "cursor_field, expecting_exception, expected_cursor_field",
    [
        (None, False, "_airbyte_emitted_at"),
        (["updated_at"], False, "updated_at"),
        (["_airbyte_emitted_at"], False, "_airbyte_emitted_at"),
        (["parent", "nested_field"], True, "nested_field"),
    ],
)
def test_cursor_field(cursor_field: List[str], expecting_exception: bool, expected_cursor_field: str):
    stream_processor = StreamProcessor.create(
        stream_name="test_cursor_field",
        destination_type=DestinationType.POSTGRES,
        raw_schema="raw_schema",
        schema="schema_name",
        source_sync_mode=SyncMode.incremental,
        destination_sync_mode=DestinationSyncMode.append_dedup,
        cursor_field=cursor_field,
        primary_key=[],
        json_column_name="json_column_name",
        properties=[],
        tables_registry=set(),
        from_table="",
    )
    try:
        assert (
            stream_processor.get_cursor_field(column_names={expected_cursor_field: (expected_cursor_field, "random")})
            == expected_cursor_field
        )
    except ValueError as e:
        if not expecting_exception:
            raise e


@pytest.mark.parametrize(
    "primary_key, column_type, expecting_exception, expected_primary_keys, expected_final_primary_key_string",
    [
        ([["id"]], "string", False, ["id"], "{{ adapter.quote('id') }}"),
        ([["first_name"], ["last_name"]], "string", False, ["first_name", "last_name"], "first_name, last_name"),
        ([["float_id"]], "number", False, ["float_id"], "cast({{ 'float_id' }} as {{ dbt_utils.type_string() }})"),
        ([["_airbyte_emitted_at"]], "string", False, [], "cast(_airbyte_emitted_at as {{ dbt_utils.type_string() }})"),
        (None, "string", True, [], ""),
        ([["parent", "nested_field"]], "string", True, [], ""),
    ],
)
def test_primary_key(
    primary_key: List[List[str]],
    column_type: str,
    expecting_exception: bool,
    expected_primary_keys: List[str],
    expected_final_primary_key_string: str,
):
    stream_processor = StreamProcessor.create(
        stream_name="test_primary_key",
        destination_type=DestinationType.POSTGRES,
        raw_schema="raw_schema",
        schema="schema_name",
        source_sync_mode=SyncMode.incremental,
        destination_sync_mode=DestinationSyncMode.append_dedup,
        cursor_field=[],
        primary_key=primary_key,
        json_column_name="json_column_name",
        properties={key: {"type": column_type} for key in expected_primary_keys},
        tables_registry=set(),
        from_table="",
    )
    try:
        assert stream_processor.get_primary_key(column_names=stream_processor.extract_column_names()) == expected_final_primary_key_string
    except ValueError as e:
        if not expecting_exception:
            raise e
