# iNSync Companion Protocol v1

Default endpoint: `http://<ps4-ip>:49560`

## Pairing

`POST /v1/pair/request`

```json
{"request_id":"<client nonce>","client":"ATHENA"}
```

Returns `waiting`; the console must physically approve with Cross.

`GET /v1/pair/status?request_id=<nonce>`

Returns `waiting`, `approved` with token, or `rejected`.

All other endpoints require:

`Authorization: Bearer <token>`

## Status

`GET /v1/status`

## Queue

- `GET /v1/queue`
- `POST /v1/queue/add` with `{"url":"http://pc:port/pkg/...","name":"Game.pkg","size":123}`
- `POST /v1/queue/<id>/pause`
- `POST /v1/queue/<id>/resume`
- `POST /v1/queue/<id>/cancel`
- `POST /v1/queue/<id>/top`

Only the queue head is registered with BGFT automatically. Pending items remain in the companion queue, which makes reordering deterministic.

## Installed titles

`GET /v1/games`

Returns read-only title metadata recovered from `/user/appmeta/<TITLE_ID>/param.sfo`.
