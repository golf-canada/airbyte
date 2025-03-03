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


import inspect
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Generator, List, Mapping, Tuple

from airbyte_protocol import AirbyteStream, ConfiguredAirbyteCatalog, ConfiguredAirbyteStream, SyncMode

from .schema_helpers import ResourceSchemaLoader


def package_name_from_class(cls: object) -> str:
    """Find the package name given a class name"""
    module = inspect.getmodule(cls)
    return module.__name__.split(".")[0]


class StreamStateMixin:
    def get_stream_state(self, name: str) -> Any:
        """Get state of stream with corresponding name"""
        raise NotImplementedError

    def set_stream_state(self, name: str, state: Any):
        """Set state of stream with corresponding name"""
        raise NotImplementedError

    def stream_has_state(self, name: str) -> bool:
        """Tell if stream supports incremental sync"""
        return False


class BaseClient(StreamStateMixin, ABC):
    """Base client for API"""

    schema_loader_class = ResourceSchemaLoader

    def __init__(self, **kwargs):
        package_name = package_name_from_class(self.__class__)
        self._schema_loader = self.schema_loader_class(package_name)
        self._stream_methods = self._enumerate_methods()

    def _enumerate_methods(self) -> Mapping[str, callable]:
        """Detect available streams and return mapping"""
        prefix = "stream__"
        mapping = {}
        methods = inspect.getmembers(self.__class__, predicate=inspect.isfunction)
        for name, method in methods:
            if name.startswith(prefix):
                mapping[name[len(prefix) :]] = getattr(self, name)

        return mapping

    @staticmethod
    def _get_fields_from_stream(stream: AirbyteStream) -> List[str]:
        return list(stream.json_schema.get("properties", {}).keys())

    def _get_stream_method(self, name: str) -> Callable:
        method = self._stream_methods.get(name)
        if not method:
            raise ValueError(f"Client does not know how to read stream `{name}`")
        return method

    def read_stream(self, stream: AirbyteStream) -> Generator[Dict[str, Any], None, None]:
        """Yield records from stream"""
        method = self._get_stream_method(stream.name)
        fields = self._get_fields_from_stream(stream)

        for message in method(fields=fields):
            yield dict(message)

    @property
    def streams(self) -> Generator[AirbyteStream, None, None]:
        """List of available streams"""
        for name, method in self._stream_methods.items():
            supported_sync_modes = [SyncMode.full_refresh]
            source_defined_cursor = False
            if self.stream_has_state(name):
                supported_sync_modes += [SyncMode.incremental]
                source_defined_cursor = True

            yield AirbyteStream(
                name=name,
                json_schema=self._schema_loader.get_schema(name),
                supported_sync_modes=supported_sync_modes,
                source_defined_cursor=source_defined_cursor,
            )

    @abstractmethod
    def health_check(self) -> Tuple[bool, str]:
        """Check if service is up and running"""


def configured_catalog_from_client(client: BaseClient) -> ConfiguredAirbyteCatalog:
    """Helper to generate configured catalog for testing"""
    catalog = ConfiguredAirbyteCatalog(streams=[ConfiguredAirbyteStream(stream=stream) for stream in client.streams])

    return catalog
