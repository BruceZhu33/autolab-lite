"use client";

/** 采纳为新基线的确认按钮:基线快照会被本次实测覆盖(旧值入历史),一步不可逆故确认。 */

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Loader2, TrendingUp } from "lucide-react";
import { adoptValidation, type Project } from "@/lib/api";
import { Button } from "~/vendor/ui/components/ui/button";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
  AlertDialogTrigger,
} from "~/vendor/ui/components/ui/alert-dialog";

export function AdoptButton({ project, runId }: { project: Project; runId: string }) {
  const queryClient = useQueryClient();
  const [adopting, setAdopting] = useState(false);

  const adopt = async () => {
    setAdopting(true);
    try {
      const res = await adoptValidation(project.id, runId);
      const n = Object.keys(res.adopted.baseline).length;
      toast.success(`已采纳为新基线(${n} 个数据集)`);
      void queryClient.invalidateQueries({ queryKey: ["run-latest", project.id] });
      void queryClient.invalidateQueries({ queryKey: ["datasets", project.id] });
    } catch (err) {
      toast.error(`采纳失败:${(err as Error).message}`);
    } finally {
      setAdopting(false);
    }
  };

  return (
    <AlertDialog>
      <AlertDialogTrigger render={
        <Button size="sm" className="gap-1" disabled={adopting} />}>
        {adopting ? <Loader2 className="size-3.5 animate-spin" /> : <TrendingUp className="size-3.5" />}
        采纳为新基线
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>采纳为新的基线?</AlertDialogTitle>
          <AlertDialogDescription>
            每个数据集的基线指标将更新为本次验证的实测均值(未实测的保持旧值),
            旧快照存入历史可回看;下一轮研究会话将以新基线为对比对象。
            确认新基线与写研究日志同时完成,此操作不可自动撤销。
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>再想想</AlertDialogCancel>
          <AlertDialogAction onClick={() => void adopt()}>确认采纳</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
