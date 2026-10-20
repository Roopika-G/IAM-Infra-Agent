import { NavLink, Route, Routes } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import History from './pages/History'
import IncidentDetail from './pages/IncidentDetail'

export default function App() {
  return (
    <div className="shell">
      <nav className="sidebar">
        <div className="brand">
          <div className="brand-name">Self-Healing IAM</div>
          <div className="brand-sub">kind-self-healing-iam</div>
        </div>
        <NavLink to="/" end>Dashboard</NavLink>
        <NavLink to="/history">History</NavLink>
        <div className="sidebar-foot">localhost only, no auth</div>
      </nav>
      <main className="content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/history" element={<History />} />
          <Route path="/incidents/:id" element={<IncidentDetail />} />
          <Route path="*" element={<p className="muted">Page not found.</p>} />
        </Routes>
      </main>
    </div>
  )
}
