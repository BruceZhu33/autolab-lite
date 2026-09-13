'use client';
export default function GlobalError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <html lang='zh-CN'>
      <body style={{ fontFamily: 'system-ui', padding: 40 }}>
        <h2>出错了</h2>
        <button onClick={() => reset()}>重试</button>
      </body>
    </html>
  );
}
