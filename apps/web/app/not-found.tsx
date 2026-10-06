import Link from "next/link";
import { SearchX } from "lucide-react";

/**
 * 全局 404:Next 默认的 404 是低对比灰字、无返回动作,在深色画布上几乎
 * 不可见。换成正常的对比度 + 一条回去的路。
 */
export default function NotFound() {
  return (
    <div className="flex h-full flex-1 flex-col items-center justify-center gap-1 p-8 text-center">
      <span className="flex size-12 items-center justify-center rounded-full bg-muted/50">
        <SearchX className="size-6 text-muted-foreground" />
      </span>
      <h1 className="mt-4 text-title-sm font-semibold">页面不存在</h1>
      <p className="mt-1 max-w-sm text-caption text-muted-foreground">
        这个地址没有对应的内容——可能已移动,或从未存在。
      </p>
      <Link
        href="/"
        className="mt-4 inline-flex h-8 items-center gap-1.5 rounded-lg bg-primary px-3 text-body font-medium text-primary-foreground hover:bg-primary/80"
      >
        返回实验看板
      </Link>
    </div>
  );
}
