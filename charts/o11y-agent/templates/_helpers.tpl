{{/*
Expand the name of the chart.
*/}}
{{- define "o11y-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "o11y-agent.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "o11y-agent.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/name: {{ include "o11y-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "o11y-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "o11y-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name
*/}}
{{- define "o11y-agent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "o11y-agent.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Secret name for Splunk credentials
*/}}
{{- define "o11y-agent.splunkSecretName" -}}
{{- if .Values.splunk.existingSecret }}
{{- .Values.splunk.existingSecret }}
{{- else }}
{{- include "o11y-agent.fullname" . }}-splunk
{{- end }}
{{- end }}

{{/*
Secret name for AWS credentials
*/}}
{{- define "o11y-agent.awsSecretName" -}}
{{- if .Values.aws.existingSecret }}
{{- .Values.aws.existingSecret }}
{{- else }}
{{- include "o11y-agent.fullname" . }}-aws
{{- end }}
{{- end }}
