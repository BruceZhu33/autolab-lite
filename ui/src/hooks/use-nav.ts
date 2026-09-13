'use client';

import { useMemo } from 'react';
import type { NavGroup } from '@/types';

// AutoLab：单用户桌面产品，无 RBAC，导航直接透传。
export function useFilteredNavGroups(groups: NavGroup[]) {
  return useMemo(() => groups.filter((g) => g.items.length > 0), [groups]);
}
