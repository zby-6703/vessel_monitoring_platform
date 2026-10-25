import axios from 'axios'

export interface RecordItem {
  id: number
  captured_at: string
  track_id: number
  camera_id: string
  ship_name: string
  mmsi: string | null
  draft_depth: number | null
  displacement_tons: number | null
  load_ratio: number | null
  risk_level: 'normal' | 'warning' | 'critical' | 'unknown'
  confidence: number | null
  full_image_path: string | null
  ship_name_image_path: string | null
  water_mask_path: string | null
  draft_image_path: string | null
  review_status: 'pending' | 'confirmed' | 'rejected'
}

export interface PaginatedRecords {
  items: RecordItem[]
  total: number
  page: number
  page_size: number
  pages: number
}

export interface HourlyMetric { time: string; traffic: number; average_draft: number | null }
export interface Statistics {
  generated_at: string
  today_traffic: number
  overload_alerts: number
  average_draft: number | null
  average_displacement: number | null
  hourly: HourlyMetric[]
}

export interface ModelSlot { name: string; status: string; config: string | null; weights: string | null }
export interface Health { status: string; realtime_status: string; environment: string; demo_mode: boolean; device: string; models: ModelSlot[]; dependencies: { database: string; redis: 'ready' | 'optional' | 'unavailable' } }
export interface StreamItem { id: string; name: string; status: 'configured' | 'unconfigured'; protocol: string | null; play_url: string | null }
export interface LocalPlaybackStatus { status: 'idle' | 'starting' | 'running' | 'completed' | 'stopped' | 'failed'; session_id: string | null; filename: string | null; camera_id?: string; frame_id: number; total_frames: number; frame_step?: number; error: string | null }
export type OfflineTask = 'region_detection' | 'ship_name_recognition' | 'draft_estimation' | 'full_pipeline'
export type OfflineJobStatus = 'uploading' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
export interface OfflineJobItem {
  filename: string
  upload_name: string
  status: OfflineJobStatus
  progress: number
  error: string | null
  result_uri: string | null
  visual_uri: string | null
}
export interface OfflineArtifact {
  uri: string
  kind: string
  mime_type: string
  bytes: number
  sha256: string
}
export interface OfflineManifest {
  schema_version: '2.0'
  job_id: string
  item_id: string
  task: OfflineTask
  options: OfflineJob['options']
  source_name: string
  source_hash: string | null
  mode: 'image' | 'video' | 'image_sequence'
  processed_samples: number
  elapsed_seconds: number
  artifacts: OfflineArtifact[]
}
export interface OfflineJob {
  id: string
  status: OfflineJobStatus
  progress: number
  error: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  options: {
    task: OfflineTask
    enable_tracking: boolean
    frame_step: number
    max_frames: number | null
    visualize: boolean
  }
  items: OfflineJobItem[]
}
export interface DashboardData { statistics: Statistics; records: RecordItem[] }
export interface RealtimeResultItem {
  id: number; session_id: string; source_type: string; source_name: string | null; camera_id: string | null
  track_id: number | null; frame_index?: number; captured_at: string; start_time?: string
  ship_name: string; mmsi: string | null; draft_depth: number | null; status: string; confidence?: number | null
  bounding_box?: Record<string, unknown> | null
}
export interface RealtimeResults { frames: RealtimeResultItem[]; instances: RealtimeResultItem[] }
export interface ModelSettings {
  values: Record<string, string>
  sources: Record<string, 'override' | 'environment'>
  override_file: string
  message?: string
}

export interface RecordingSettings {
  enabled: boolean
  pre_roll_seconds: number
  post_roll_seconds: number
  min_hits: number
  confidence_threshold: number
  max_clip_seconds: number
  max_storage_gb: number
  retention_days: number
  focus_mode: 'auto' | 'manual'
  zoom_factor: number
  center_x: number
  center_y: number
  padding: number
}
export const defaultRecordingSettings = (): RecordingSettings => ({
  enabled: false, pre_roll_seconds: 3, post_roll_seconds: 5, min_hits: 2,
  confidence_threshold: .45, max_clip_seconds: 300, max_storage_gb: 10,
  retention_days: 30, focus_mode: 'auto', zoom_factor: 2, center_x: .5, center_y: .5, padding: .2,
})
export interface RecordingStatus {
  state: 'disabled' | 'armed' | 'recording' | 'error'
  active_id: string | null
  buffer_seconds: number
  error: string | null
  source_name: string | null
  storage_bytes: number
  zoom_capability: 'digital'
  focus: { x: number; y: number; width: number; height: number } | null
}
export interface RecordingItem {
  id: string
  camera_id: string
  source_name: string
  started_at: string
  ended_at: string | null
  duration_seconds: number
  status: 'completed' | 'interrupted' | 'failed' | 'recording' | 'processing'
  reason: string | null
  ship_names: string[]
  track_ids: number[]
  bytes: number
  original_url: string | null
  focused_url: string | null
  thumbnail_url: string | null
  has_focus: boolean
}
export interface CameraStatus {
  status: 'idle' | 'starting' | 'running' | 'stopped' | 'failed'
  configured: boolean
  session_id: string | null
  camera_id: string
  filename: string | null
  frame_id: number
  error: string | null
}
export interface PtzStatus { configured: boolean; supported: boolean; focus_supported?: boolean; error: string | null }
export type PtzAction = 'zoom_in' | 'zoom_out' | 'focus_near' | 'focus_far' | 'autofocus' | 'stop' | 'home'

export function getRecordingSettings() { return api.get<RecordingSettings>('/recording/settings').then(({ data }) => data) }
export function updateRecordingSettings(settings: RecordingSettings) { return api.put<RecordingSettings>('/recording/settings', settings).then(({ data }) => data) }
export function getRecordingStatus() { return api.get<RecordingStatus>('/recording/status').then(({ data }) => data) }
export function getRecordings(params: Record<string, unknown> = {}) {
  return api.get<{ items: RecordingItem[]; total: number; page: number; page_size: number }>('/recordings', { params }).then(({ data }) => data)
}
export function deleteRecording(id: string) { return api.delete(`/recordings/${encodeURIComponent(id)}`) }
export function getCameraStatus() { return api.get<CameraStatus>('/realtime/camera').then(({ data }) => data) }
export function startCamera(frameStep = 3) { return api.post<CameraStatus>('/realtime/camera/start', { frame_step: frameStep }, { timeout: 0 }).then(({ data }) => data) }
export function stopCamera() { return api.post<CameraStatus>('/realtime/camera/stop').then(({ data }) => data) }
export function getPtzStatus() { return api.get<PtzStatus>('/camera/ptz').then(({ data }) => data) }
export function controlPtz(action: PtzAction) { return api.post('/camera/ptz', { action }).then(({ data }) => data) }

export function formatBytes(bytes: number) {
  if (!bytes) return '0 MB'
  return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`
}

export function apiError(error: unknown, fallback: string) {
  const detail = axios.isAxiosError(error) ? error.response?.data?.detail : null
  return typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((item: { msg?: string }) => item.msg || '参数无效').join('；') : fallback
}

export const api = axios.create({ baseURL: '/api', timeout: 10000 })

export function getRecords(params: Record<string, unknown> = {}) {
  return api.get<PaginatedRecords>('/records', { params }).then(({ data }) => data)
}

export function getStatistics() {
  return api.get<Statistics>('/statistics').then(({ data }) => data)
}

export function getHealth() {
  return api.get<Health>('/health').then(({ data }) => data)
}

export function getStreams() {
  return api.get<{ items: StreamItem[] }>('/streams').then(({ data }) => data.items)
}

export function getLocalPlayback() {
  return api.get<LocalPlaybackStatus>('/realtime/local-video').then(({ data }) => data)
}

export function startLocalPlayback(form: FormData, onProgress?: (percent: number) => void) {
  return api.post<LocalPlaybackStatus>('/realtime/local-video', form, {
    timeout: 0,
    onUploadProgress: (event) => {
      if (event.total && onProgress) onProgress(Math.round(event.loaded / event.total * 100))
    },
  }).then(({ data }) => data)
}

export function stopLocalPlayback() {
  return api.post<LocalPlaybackStatus>('/realtime/local-video/stop').then(({ data }) => data)
}

export function getOfflineJobs() {
  return api.get<{ items: OfflineJob[] }>('/jobs').then(({ data }) => data.items)
}

export function getDashboard() {
  return api.get<DashboardData>('/dashboard').then(({ data }) => data)
}

export function getRealtimeResults(limit = 50) {
  return api.get<RealtimeResults>('/realtime/results', { params: { limit } }).then(({ data }) => data)
}

export function getOfflineJob(id: string) {
  return api.get<OfflineJob>(`/jobs/${id}`).then(({ data }) => data)
}

export function createOfflineJob(form: FormData, onProgress?: (percent: number) => void) {
  return api.post<OfflineJob>('/jobs', form, {
    timeout: 0,
    onUploadProgress: (event) => {
      if (event.total && onProgress) onProgress(Math.round(event.loaded / event.total * 100))
    },
  }).then(({ data }) => data)
}

export function cancelOfflineJob(id: string) {
  return api.post<OfflineJob>(`/jobs/${id}/cancel`).then(({ data }) => data)
}

export function offlineJobDownloadUrl(jobId: string) {
  return `/api/v2/inference-jobs/${jobId}/download`
}

export function offlineArtifactUrl(jobId: string, itemId: string, uri: string) {
  const prefix = `jobs/${jobId}/items/${itemId}/output/`
  if (!uri.startsWith(prefix)) return ''
  return `/api/v2/inference-jobs/${jobId}/items/${itemId}/files/${uri.slice(prefix.length)}`
}

export function getOfflineManifest(jobId: string, itemId: string) {
  return api.get<OfflineManifest>(`/v2/inference-jobs/${jobId}/items/${itemId}/files/manifest.json`).then(({ data }) => data)
}

export function getModelSettings() {
  return api.get<ModelSettings>('/settings/models').then(({ data }) => data)
}

export function updateModelSettings(values: Record<string, string>) {
  return api.put<ModelSettings>('/settings/models', values).then(({ data }) => data)
}

export function resetModelSettings() {
  return api.delete<ModelSettings>('/settings/models').then(({ data }) => data)
}

export function updateReview(id: number, status: RecordItem['review_status']) {
  return api.patch<RecordItem>(`/records/${id}/review`, { status }).then(({ data }) => data)
}

export function assetUrl(path: string | null) {
  return path || ''
}
