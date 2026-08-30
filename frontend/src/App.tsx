import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AppLayout } from '@/components/navigation/AppLayout';
import { Landing } from '@/pages/Landing';
import { Dashboard } from '@/pages/Dashboard';
import { Queue } from '@/pages/Queue';
import { RecordDetail } from '@/pages/RecordDetail';
import { Evaluation } from '@/pages/Evaluation';
import { Upload } from '@/pages/Upload';
import { AuditTrail } from '@/pages/AuditTrail';

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route path="/app" element={<AppLayout />}>
          <Route index element={<Dashboard />} />
          <Route path="queue" element={<Queue />} />
          <Route path="records/:id" element={<RecordDetail />} />
          <Route path="evaluation" element={<Evaluation />} />
          <Route path="upload" element={<Upload />} />
          <Route path="audit" element={<AuditTrail />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
