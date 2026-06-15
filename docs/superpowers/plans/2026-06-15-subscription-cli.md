# Subscription CLI Management

## Motivation

Currently users must manually edit `config/subscriptions.toml` to add/remove/list subscriptions. This is error-prone and unfriendly.

## Requirements

- `trawler subscription add --platform bili --uid 123456 --name "UP主"`
- `trawler subscription remove --platform bili --uid 123456`
- `trawler subscription list [--platform bili]`
- Support all three platforms (bili, xhs, weibo)

## Implementation sketch

- New file: `core/subscription_cli.py` or inline in `run_check.py`
- Reads/writes `config/subscriptions.toml` using `tomlkit`
- Validates platform-specific fields (uid for bili, user_id for xhs/weibo)
- `list` outputs a Rich table

## Dependencies

- `tomlkit` already in core deps — no new deps needed.
