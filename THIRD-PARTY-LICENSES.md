# Third-Party Licenses

This project incorporates or depends on the following third-party components:

## MaiBot Plugin SDK

- **Versions checked**: 2.16.0, 2.20.0 and 2.21.0
- **License**: LGPL-3.0-only
- **Source**: https://github.com/Mai-with-u/maibot-plugin-sdk
- **Usage**: Plugin base classes, component decorators, configuration model

## websockets

- **Version**: >= 12.0
- **License**: BSD-3-Clause
- **Source**: https://github.com/aaugustin/websockets
- **Usage**: OneBot WebSocket server

## pydantic

- **Version**: >= 2.0
- **License**: MIT
- **Source**: https://github.com/pydantic/pydantic
- **Usage**: Configuration model validation

## pilk (optional)

- **Version**: 0.2.4
- **License**: GPL-3.0
- **Source**: https://github.com/foyoux/pilk
- **Usage**: SILK audio codec for voice synthesis

## pycryptodome (optional)

- **Version**: >= 3.0
- **License**: BSD-3-Clause / Public Domain
- **Source**: https://github.com/Legrandin/pycryptodome
- **Usage**: AES decryption for image fallback decoding

## MaiBot core (upstream, not bundled)

- **Compatibility range**: 1.3.16–1.3.19; 1.3.19 checked with SDK 2.20.0 in isolation
- **License**: AGPL-3.0 (default) + Special GPL License Grant for plugin ecosystem
- **Source**: https://github.com/Mai-with-u/MaiBot
- **Note**: This plugin interacts via RPC as a separate process

## Adapter reference

- **Source**: https://github.com/jessiongod/mai-bot-wechat-adapter
- **License declared by upstream**: GPL-3.0-or-later
- **Note**: Attribution previously stored in the nonstandard manifest `credits` field is retained here; the field is removed for strict Host manifest validation.

## Reader source and runtime components

- **Reader helpers** are from the [WeChatAgent 0.4.1 source release](https://github.com/sourcepro5/WeChatAgent/tree/v0.4.1). The upstream notices are in [WeChatAgent-LICENSE.txt](runtime/WeChatAgent-LICENSE.txt). The NT database and media source subset includes [weflow-cli](https://github.com/zhuobichen/weflow-cli) code. Its [MIT notice](runtime/reader/components/weflow-cli/LICENSE) is included.
- **Runtime binaries** in the full release include Tencent Weixin 4.1.10.27, the matching `version.dll` and the `wx_key.dll` account connection component from the matching upstream deployment. The binaries retain their respective terms.
