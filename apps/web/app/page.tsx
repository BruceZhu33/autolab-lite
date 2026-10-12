import { Board } from "./board";

// 初始 HTML 即含看板外壳(E2E 断言依赖);路由段配置只对 Server Component 生效,故拆此壳。
export const dynamic = "force-dynamic";

export default function BoardPage() {
  return <Board />;
}
