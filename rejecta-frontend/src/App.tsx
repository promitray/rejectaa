import type { ReactElement } from 'react'
import { Suspense, lazy } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { AuthProvider } from './auth/AuthProvider'
import { RequireAdmin, RequireAuth } from './auth/guards'
import Layout from './components/Layout'
import ToolFrame from './components/ToolFrame'
import { PageLoader } from './components/Loaders'
import Account from './pages/Account'
import Calibrate from './pages/Calibrate'
import Forge from './pages/Forge'
import Heatmap from './pages/Heatmap'
import Landing from './pages/Landing'
import Login from './pages/Login'
import Processing from './pages/Processing'
import Rebuttal from './pages/Rebuttal'
import ResetPassword from './pages/ResetPassword'
import Results from './pages/Results'
import Signup from './pages/Signup'
import Upload from './pages/Upload'

// Admin screens load on demand: regular users never download them.
const AdminLayout = lazy(() => import('./pages/admin/AdminLayout'))
const AuditLog = lazy(() => import('./pages/admin/AuditLog'))
const Features = lazy(() => import('./pages/admin/Features'))
const Overview = lazy(() => import('./pages/admin/Overview'))
const PartnerDetail = lazy(() => import('./pages/admin/PartnerDetail'))
const Partners = lazy(() => import('./pages/admin/Partners'))
const PlanEditor = lazy(() => import('./pages/admin/PlanEditor'))
const Plans = lazy(() => import('./pages/admin/Plans'))
const UsagePage = lazy(() => import('./pages/admin/Usage'))
const UserDetail = lazy(() => import('./pages/admin/UserDetail'))
const Users = lazy(() => import('./pages/admin/Users'))

export default function App(): ReactElement {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route element={<Layout />}>
            <Route path="/login" element={<Login />} />
            <Route path="/signup" element={<Signup />} />
            <Route path="/reset-password" element={<ResetPassword />} />

            <Route path="/app" element={<RequireAuth><ToolFrame feature="analyse_paper"><Upload /></ToolFrame></RequireAuth>} />
            <Route path="/processing" element={<RequireAuth><Processing /></RequireAuth>} />
            <Route path="/results" element={<RequireAuth><Results /></RequireAuth>} />
            <Route path="/calibrate" element={<RequireAuth><ToolFrame feature="voice_calibrate"><Calibrate /></ToolFrame></RequireAuth>} />
            <Route path="/heatmap" element={<RequireAuth><ToolFrame feature="ai_heatmap"><Heatmap /></ToolFrame></RequireAuth>} />
            <Route path="/forge" element={<RequireAuth><ToolFrame feature="abstract_forge"><Forge /></ToolFrame></RequireAuth>} />
            <Route path="/rebuttal" element={<RequireAuth><ToolFrame feature="rebuttal_writer"><Rebuttal /></ToolFrame></RequireAuth>} />
            <Route path="/account" element={<RequireAuth><Account /></RequireAuth>} />

            <Route path="/admin" element={<RequireAdmin><Suspense fallback={<PageLoader label="Opening the admin console" />}><AdminLayout /></Suspense></RequireAdmin>}>
              <Route index element={<Overview />} />
              <Route path="users" element={<Users />} />
              <Route path="users/:id" element={<UserDetail />} />
              <Route path="plans" element={<Plans />} />
              <Route path="plans/:id" element={<PlanEditor />} />
              <Route path="features" element={<Features />} />
              <Route path="partners" element={<Partners />} />
              <Route path="partners/:id" element={<PartnerDetail />} />
              <Route path="usage" element={<UsagePage />} />
              <Route path="audit" element={<AuditLog />} />
            </Route>
          </Route>
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}
