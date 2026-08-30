import { Outlet } from 'react-router-dom';
import { Sidebar } from './Sidebar';

export function AppLayout() {
  return (
    <div className="flex min-h-screen bg-ink-950">
      <Sidebar />
      <div className="flex-1 flex flex-col min-w-0 relative">
        <Outlet />
      </div>
    </div>
  );
}
