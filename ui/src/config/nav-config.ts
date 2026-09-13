import { NavGroup } from '@/types';

export const navGroups: NavGroup[] = [
  {
    label: 'AutoLab · 科研训练台',
    items: [
      { title: '迭代监视', url: '/dashboard', icon: 'dashboard', isActive: false, shortcut: ['g', 'm'], items: [] },
      { title: '服务器 / GPU', url: '/dashboard/gpu', icon: 'kanban', isActive: false, shortcut: ['g', 's'], items: [] },
      { title: '项目', url: '/dashboard/projects', icon: 'teams', isActive: false, shortcut: ['g', 'p'], items: [] },
      { title: '方案评审', url: '/dashboard/review', icon: 'user', isActive: false, shortcut: ['g', 'r'], items: [] },
      { title: '报告', url: '/dashboard/report', icon: 'account', isActive: false, shortcut: ['g', 'o'], items: [] }
    ]
  },
  {
    label: '系统',
    items: [
      { title: '设置', url: '/dashboard/autolab-settings', icon: 'profile', isActive: false, shortcut: ['g', 'c'], items: [] }
    ]
  }
];
