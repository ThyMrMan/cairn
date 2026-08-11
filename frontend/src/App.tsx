import { useQuery } from "@tanstack/react-query";
import { Navigate, Route, Routes } from "react-router-dom";

import Layout from "./components/Layout";
import { Spinner } from "./components/ui";
import { endpoints } from "./lib/api";
import { useAuth } from "./lib/auth";
import Dashboard from "./pages/Dashboard";
import Folders from "./pages/Folders";
import Jobs from "./pages/Jobs";
import Login from "./pages/Login";
import Profiles from "./pages/Profiles";
import Search from "./pages/Search";
import Settings from "./pages/Settings";
import Setup from "./pages/Setup";
import SiteDetail from "./pages/SiteDetail";
import Sites from "./pages/Sites";

export default function App() {
  const { user, loading, setupComplete } = useAuth();
  const setup = useQuery({
    queryKey: ["setup-status"],
    queryFn: endpoints.setupStatus,
    enabled: !setupComplete,
  });

  if (loading) {
    return (
      <div className="flex min-h-full items-center justify-center">
        <Spinner className="h-6 w-6 text-muted" />
      </div>
    );
  }

  // Three states, in order: no account yet, not signed in, signed in. The
  // setup screen is unreachable once an account exists — enforced server-side
  // too, so hiding it here is convenience rather than the control.
  if (!setupComplete) {
    return <Setup minLength={setup.data?.password_min_length ?? 12} />;
  }
  if (!user) {
    return <Login />;
  }

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="search" element={<Search />} />
        <Route path="sites" element={<Sites />} />
        <Route path="sites/:id" element={<SiteDetail />} />
        <Route path="folders" element={<Folders />} />
        <Route path="profiles" element={<Profiles />} />
        <Route path="jobs" element={<Jobs />} />
        <Route path="settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
