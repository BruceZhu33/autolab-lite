import { BoardClient } from "./board-client";

// ?stage= 下钻需要搜索参数参与 SSR;force-dynamic 让初始 HTML 即含看板
// 内容(E2E 断言依赖)。路由段配置只对 Server Component 生效,故拆此壳。
export const dynamic = "force-dynamic";

export default function BoardPage() {
  return <BoardClient />;
}
