'use client';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu';

export function UserNav() {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant='ghost' className='h-8 w-8 rounded-full' />}>
        <span className='flex size-8 items-center justify-center rounded-full bg-primary/10 text-sm font-semibold text-primary'>
          AL
        </span>
      </DropdownMenuTrigger>
      <DropdownMenuContent className='w-56' align='end' sideOffset={10}>
        <DropdownMenuLabel className='font-normal'>
          <div className='flex flex-col space-y-1'>
            <p className='text-sm leading-none font-medium'>本地单用户</p>
            <p className='text-xs leading-none text-muted-foreground'>AutoLab 自用工具（D1）</p>
          </div>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem disabled>设置</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
