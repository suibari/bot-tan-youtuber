# YouTube streamList protocol

`stream_list.proto` is the Google code sample from:
https://developers.google.com/youtube/v3/live/streaming-live-chat

Retrieved 2026-09-06. Google documentation code samples are licensed under
Apache License 2.0: https://www.apache.org/licenses/LICENSE-2.0
The missing `google/protobuf/duration.proto` import was added so the sample
compiles. Otherwise the schema is preserved as published. It does not define
messageDeletedEvent / messageRetractedEvent. Tombstones can identify messages
already deleted, but are not guaranteed to be sent at deletion time.

Generate from the repository root (grpcio-tools is a build-only dependency):

```sh
venv/bin/python -m pip install grpcio-tools==1.83.1
venv/bin/python -m grpc_tools.protoc -I. --python_out=. common/youtube_stream_proto/stream_list.proto
```

The transport uses `channel.unary_stream`, so no generated server/client stub
module is needed. Commit the generated `stream_list_pb2.py` with the schema.
