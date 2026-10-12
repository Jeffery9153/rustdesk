#!/usr/bin/env python3
"""向 RustDesk 源码注入自建服务器配置，并关闭软件更新相关功能。

CI 在 checkout 之后、任何编译步骤之前运行本脚本。所有敏感值都通过环境变量
传入，仓库里不保存任何密钥：

    RD_SERVERS   ID/中继服务器，例如 rdp.example.com:31116
    RD_PUBKEY    服务器公钥（base64，ed25519）
    RD_API_URL   自建 API 服务器地址

本地构建也可以用同一脚本，注入后再编译：

    RD_SERVERS=... RD_PUBKEY=... RD_API_URL=... \
        python3 .github/scripts/inject_custom_client.py

每一处替换都断言命中次数，末尾再做一次 grep 复核；只要有一处对不上就
exit 1 —— 宁可让构建失败，也绝不静默产出未定制的官方版客户端。
"""

import os
import re
import sys

CFG = "libs/hbb_common/src/config.rs"
COMMON = "src/common.rs"
UPDATER = "src/updater.rs"
DESKTOP_SETTINGS = "flutter/lib/desktop/pages/desktop_setting_page.dart"
MOBILE_SETTINGS = "flutter/lib/mobile/pages/settings_page.dart"


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"[inject] 错误：环境变量 {name} 为空或未提供")
    return value


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        # 统一成 LF，保证跨平台（Windows runner）也能匹配多行模式
        return f.read().replace("\r\n", "\n")


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def patch(path: str, pattern: str, repl, label: str, count: int = 1) -> None:
    """正则替换并断言命中次数，失败即中止。"""
    src = read(path)
    new, hits = re.subn(pattern, repl, src, count=count)
    if hits != count:
        sys.exit(f"[inject] 错误：{label} —— 预期在 {path} 命中 {count} 处，实际 {hits} 处")
    write(path, new)
    print(f"[inject] {label}：已应用（{hits} 处）")


def assert_absent(path: str, needle: str, label: str) -> None:
    if needle in read(path):
        sys.exit(f"[inject] 错误：{label} 复核失败，{path} 中仍存在 {needle!r}")


def assert_present(path: str, needle: str, label: str) -> None:
    if needle not in read(path):
        sys.exit(f"[inject] 错误：{label} 复核失败，{path} 中找不到 {needle!r}")


def _force_utf8_stdio() -> None:
    """把标准输出/错误强制成 UTF-8。

    Windows runner 的控制台默认编码不是 UTF-8（cp1252 / cp437 等），本脚本打印的
    中文标签会抛 UnicodeEncodeError 让整个构建失败（已在 windows-2022 上实测复现）。
    这里就地改掉编码；万一仍有无法编码的字符，用 replace 兜底 —— 日志可读性可以
    让步，但绝不能因为日志编码问题中断构建。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> None:
    _force_utf8_stdio()

    servers = require_env("RD_SERVERS")
    pubkey = require_env("RD_PUBKEY")
    api_url = require_env("RD_API_URL")

    # ---- 1. ID/中继服务器 + 公钥 -------------------------------------------
    # 锚点用常量名而不是上游默认值：上游哪天改了默认值也不会失效。
    patch(
        CFG,
        r'(pub const RENDEZVOUS_SERVERS: &\[&str\] = &\[")[^"]*("\];)',
        lambda m: m.group(1) + servers + m.group(2),
        "RENDEZVOUS_SERVERS",
    )
    patch(
        CFG,
        r'(pub const RS_PUB_KEY: &str = ")[^"]*(";)',
        lambda m: m.group(1) + pubkey + m.group(2),
        "RS_PUB_KEY",
    )

    # ---- 2. 自建 API 服务器 ------------------------------------------------
    # 锚点必须带 .to_owned()：同一个字面量在上游的「公共服务器识别列表」
    # (src/common.rs) 中也出现过，那里绝不能动。
    patch(
        COMMON,
        r'"https://admin\.rustdesk\.com"\.to_owned\(\)',
        f'"{api_url}".to_owned()',
        "api_server",
    )

    # ---- 3. 彻底关闭软件更新检查 -------------------------------------------
    # check_software_update() 是唯一闸门：桌面端的「有新版本」卡片和移动端的
    # 更新按钮都由它触发（读同一个 updateUrl）。
    # 整函数替换成空实现，避免留下 unreachable code 警告；
    # [\s\S]*? 到第一个行首的 } 为止，即函数体末尾。
    patch(
        COMMON,
        r'pub fn check_software_update\(\) \{[\s\S]*?\n\}\n',
        "pub fn check_software_update() {\n"
        "    // 定制版：软件更新检查已被有意关闭。\n"
        "}\n",
        "禁用更新检查",
    )

    # ---- 4. 彻底关闭自动更新 -----------------------------------------------
    # updater.rs 里两处门禁：非手动更新的自动检查、macOS 由 root 服务执行的
    # 自动更新。改成 false 后两者都不会再往下走。
    patch(
        UPDATER,
        r'config::Config::get_bool_option\(keys::OPTION_ALLOW_AUTO_UPDATE\)',
        "false",
        "禁用自动更新",
        count=2,
    )

    # ---- 5. 隐藏设置界面里的更新开关 ---------------------------------------
    # 桌面端：更新检查开关 + 自动更新开关
    patch(
        DESKTOP_SETTINGS,
        r'if \(!isWeb && !bind\.isCustomClient\(\)\)',
        "if (false)",
        "隐藏桌面端更新检查开关",
    )
    patch(
        DESKTOP_SETTINGS,
        r'final showAutoUpdate = \(isWindows && bind\.mainIsInstalled\(\)\) \|\|\n'
        r'    \(isMacOS && bind\.mainIsInstalled\(\) && '
        r'bind\.mainIsInstalledDaemon\(prompt: false\) && !bind\.isCustomClient\(\)\);',
        "final showAutoUpdate = false;",
        "隐藏桌面端自动更新开关",
    )
    # 移动端：更新检查开关
    patch(
        MOBILE_SETTINGS,
        r'if \(!bind\.isCustomClient\(\)\) \{',
        "if (false) {",
        "隐藏移动端更新检查开关",
    )

    # ---- 6. 最终复核 -------------------------------------------------------
    assert_present(CFG, servers, "RENDEZVOUS_SERVERS")
    assert_present(CFG, pubkey, "RS_PUB_KEY")
    assert_present(COMMON, api_url, "api_server")
    assert_absent(COMMON, "OPTION_ENABLE_CHECK_UPDATE", "更新检查已移除")
    assert_absent(UPDATER, "OPTION_ALLOW_AUTO_UPDATE", "自动更新门禁已移除")

    print("[inject] 全部定制已应用并通过复核")


if __name__ == "__main__":
    main()
