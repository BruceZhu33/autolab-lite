"use client";

/**
 * 平台助手（全局对话入口，v0.3：替换 Multica 遗留 ChatPage——那个绑定
 * Multica chat 会话体系，在 AutoLab 场景无后端）。
 * 与 GPU 页内嵌助手共用 features/autolab/assistant-chat：每轮对话由
 * sidecar 注入实时快照（服务器/GPU/在途 run/项目/状态事件），LLM 走
 * round_analysis 使用点（deviation #35）。
 */
import { Bot } from "lucide-react";

import { PageHeader } from "@multica/views/layout/page-header";
import { AssistantChat } from "@/features/autolab/assistant-chat";

export default function ChatPage() {
  return (
    <>
      <PageHeader>
        <Bot className="size-4 text-muted-foreground" />
        <h1 className="text-body font-medium">平台助手</h1>
        <p className="ml-2 hidden min-w-0 truncate text-caption text-muted-foreground md:block">
          服务器状态 · 在途任务 · 项目阶段——也可以当常规 AI 助手用
        </p>
      </PageHeader>
      <div className="flex min-h-0 flex-1 p-4 md:px-6">
        <AssistantChat storageKey="autolab.assistant.v1" className="min-h-0 min-w-0 flex-1" />
      </div>
    </>
  );
}
