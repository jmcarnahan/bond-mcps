{{/*
Expand the name of the chart (overridable via nameOverride).
*/}}
{{- define "mcp-service.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fullname: <release>-<name>, truncated to 63 chars (k8s DNS-1123 limit).
Overridable via fullnameOverride. Release name alone if it already contains the chart name.
*/}}
{{- define "mcp-service.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart label (name-version), DNS-safe.
*/}}
{{- define "mcp-service.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Standard recommended labels.
*/}}
{{- define "mcp-service.labels" -}}
helm.sh/chart: {{ include "mcp-service.chart" . }}
{{ include "mcp-service.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: bond-mcps
{{- end -}}

{{/*
Selector labels (immutable; do not put version-y stuff here).
*/}}
{{- define "mcp-service.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mcp-service.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Service account name to use.
*/}}
{{- define "mcp-service.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- include "mcp-service.fullname" . -}}
{{- else -}}
default
{{- end -}}
{{- end -}}

{{/*
envFrom block listing the ConfigMap and every enabled secret's k8s Secret.
Used by both the main container and the migration initContainer so they see
the same environment.
*/}}
{{- define "mcp-service.envFrom" -}}
- configMapRef:
    name: {{ include "mcp-service.fullname" . }}-config
{{- if .Values.secrets.encryptionKey.enabled }}
- secretRef:
    name: {{ include "mcp-service.fullname" . }}-encryption-key
{{- end }}
{{- if .Values.secrets.db.enabled }}
- secretRef:
    name: {{ include "mcp-service.fullname" . }}-db
{{- end }}
{{- if .Values.secrets.oauth.enabled }}
- secretRef:
    name: {{ include "mcp-service.fullname" . }}-oauth
{{- end }}
{{- if .Values.jwt.enabled }}
- secretRef:
    name: {{ include "mcp-service.fullname" . }}-jwt
{{- end }}
{{- end -}}
