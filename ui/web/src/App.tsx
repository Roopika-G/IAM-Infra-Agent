import { NavLink, Route, Routes } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import IncidentDetail from './pages/IncidentDetail'
import Approvals from './pages/Approvals'
import Simulations from './pages/Simulations'

export default function App() {
  return (
    <div className="shell">
      <nav className="sidebar">
        <div className="brand">
          <div className="brand-name">Self-Healing IAM</div>
          <div className="brand-sub">kind-self-healing-iam</div>
        </div>
        <NavLink to="/" end>Dashboard</NavLink>
        <NavLink to="/approvals">Approvals</NavLink>
        <NavLink to="/simulations">Simulations</NavLink>
        <div className="sidebar-foot">localhost only, no auth</div>
      </nav>
      <main className="content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/incidents/:id" element={<IncidentDetail />} />
          <Route path="/approvals" element={<Approvals />} />
          <Route path="/simulations" element={<Simulations />} />
          <Route path="*" element={<p className="muted">Page not found.</p>} />
        </Routes>
      </main>
    </div>
  )
}
