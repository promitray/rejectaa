import type { ReactElement } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout'
import Landing from './pages/Landing'
import Processing from './pages/Processing'
import Results from './pages/Results'
import Upload from './pages/Upload'
import Calibrate from './pages/Calibrate'
import Heatmap from './pages/Heatmap'
import Forge from './pages/Forge'
import Rebuttal from './pages/Rebuttal'
export default function App(): ReactElement {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Landing />} />
        <Route element={<Layout />}>
          <Route path="/app" element={<Upload />} />
          <Route path="/processing" element={<Processing />} />
          <Route path="/results" element={<Results />} />
          <Route path="/calibrate" element={<Calibrate />} />
          <Route path="/heatmap" element={<Heatmap />} />
          <Route path="/forge" element={<Forge />} />
          <Route path="/rebuttal" element={<Rebuttal />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
