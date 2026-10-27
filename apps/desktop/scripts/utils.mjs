
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

// returns true if the passsed file is being invoked from node,
// not imported.
export function isMain(importMetaUrl) {
    return   importMetaUrl === pathToFileURL(process.argv[1]).href;
}

// 打包出来的可执行文件基名（Windows 是 <name>.exe，macOS 是 Contents/MacOS/<name>）。
//
// electron-builder 一般会通过 hook 的 context 把它传进来，但两个 hook 里原本都写着
// `|| 'Hermes'` 兜底 —— 上游的名字。真走到兜底那一步时，after-pack 会去戳一个不存在
// 的 Hermes.exe（stamp 静默失败，成品留着 stock 图标），before-pack 的回滚备份则会
// 认不出上一版的目录。两处都是「不报错、但没干活」。
//
// 兜底改成读 package.json 的 build.executableName —— 那本来就是这个名字的真源。
export function productExecutableName(platform = process.platform) {
    const desktopRoot = dirname(dirname(fileURLToPath(import.meta.url)));
    const pkg = JSON.parse(readFileSync(join(desktopRoot, 'package.json'), 'utf8'));
    const platformKey = platform === 'darwin' ? 'mac' : platform === 'win32' ? 'win' : 'linux';
    const name = pkg.build?.[platformKey]?.executableName || pkg.build?.executableName;

    if (!name) {
        throw new Error(`package.json executableName is missing for ${platform}`);
    }

    return name;
}
