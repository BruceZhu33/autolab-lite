/**
 * 开发桥：读本机 sidecar runtime.json + capability_token（D46/B2：loopback 随机端口）。
 * 生产静态导出由 Tauri 壳注入配置，不经过本路由。
 */
import { NextResponse } from 'next/server';
import { readFile } from 'fs/promises';
import path from 'path';
import os from 'os';

export const dynamic = 'force-dynamic';

function dataDir(): string {
  return (
    process.env.AUTOLAB_HOME ||
    path.join(
      os.homedir(),
      process.platform === 'darwin'
        ? 'Library/Application Support/AutoLab'
        : process.platform === 'win32'
          ? 'AppData/Roaming/AutoLab'
          : '.local/share/autolab'
    )
  );
}

export async function GET() {
  try {
    const dir = dataDir();
    const runtime = JSON.parse(await readFile(path.join(dir, 'runtime.json'), 'utf-8'));
    const token = (await readFile(path.join(dir, 'capability_token'), 'utf-8')).trim();
    return NextResponse.json({ baseUrl: `http://127.0.0.1:${runtime.port}`, token });
  } catch {
    return NextResponse.json({ error: 'runtime.json not found' }, { status: 503 });
  }
}
