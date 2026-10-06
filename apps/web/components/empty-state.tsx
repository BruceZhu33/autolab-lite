import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

/**
 * 统一空状态:竖直居中、圆底图标、标题 + 一句话说明,必须带下一步动作
 * (action 主 CTA / secondary 次链接)。空状态是死胡同时,用户只能回侧栏
 * 自己找路——每个空状态都应回答「接下来去哪」。
 *
 * 视觉沿用各页既有空状态语言(size-12 圆底图标 + text-title-sm 标题),
 * 只是收拢成一个组件,保证 10 个页面的空状态同一种语法。
 */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  secondary,
}: {
  icon: LucideIcon;
  title: string;
  description?: ReactNode;
  /** 主 CTA:通常是一个 Button(到下一步该去的页面)。 */
  action?: ReactNode;
  /** 次要入口:文字链接。 */
  secondary?: ReactNode;
}) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-1 p-8 text-center">
      <span className="flex size-12 items-center justify-center rounded-full bg-muted/50">
        <Icon className="size-6 text-muted-foreground" />
      </span>
      <h2 className="mt-4 text-title-sm font-semibold">{title}</h2>
      {description ? (
        <p className="mt-1 max-w-sm text-caption leading-relaxed text-muted-foreground">
          {description}
        </p>
      ) : null}
      {action ? <div className="mt-4">{action}</div> : null}
      {secondary ? <div className="mt-2">{secondary}</div> : null}
    </div>
  );
}
