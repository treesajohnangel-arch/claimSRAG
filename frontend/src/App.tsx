import { Link, Navigate, Route, Routes } from "react-router";
import { SessionGate } from "./auth/SessionGate";
import { AppLayout } from "./components/AppLayout";
import { PageHeader } from "./components/PageHeader";
import { ChatPage } from "./features/chat/ChatPage";
import { DashboardPage } from "./features/dashboard/DashboardPage";
import { DocumentDetailPage, DocumentsPage } from "./features/documents/DocumentsPage";
import { PolicyDetailPage } from "./features/policies/PolicyDetailPage";
import { NewPolicyPage, PoliciesPage } from "./features/policies/PoliciesPage";
import { SearchPage } from "./features/search/SearchPage";

function NotFoundPage() {
  return (
    <PageHeader title="Page not found" description={<>This page doesn't exist. Go to the <Link to="/">overview</Link>.</>} />
  );
}

export function App() {
  return (
    <Routes>
      {/* There is no sign-in (D-036): old sign-in and registration links open the workspace. */}
      <Route path="/login" element={<Navigate to="/" replace />} />
      <Route path="/register" element={<Navigate to="/" replace />} />
      <Route element={<SessionGate />}>
        <Route element={<AppLayout />}>
          <Route index element={<DashboardPage />} />
          <Route path="policies" element={<PoliciesPage />} />
          <Route path="policies/new" element={<NewPolicyPage />} />
          <Route path="policies/:policyId" element={<PolicyDetailPage />} />
          <Route path="documents" element={<DocumentsPage />} />
          <Route path="documents/:documentId" element={<DocumentDetailPage />} />
          <Route path="search" element={<SearchPage />} />
          <Route path="ask" element={<ChatPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Route>
    </Routes>
  );
}
