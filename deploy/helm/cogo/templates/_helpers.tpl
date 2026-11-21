{{/* ------------------------------------------------------------------ names */}}

{{- define "cogo.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cogo.fullname" -}}
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

{{/* Resource name of a component: (dict "root" $ "name" "auth-api") -> cogo-auth-api */}}
{{- define "cogo.componentName" -}}
{{- printf "%s-%s" (include "cogo.fullname" .root) .name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "cogo.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "cogo.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* The port every CoGo image listens on (their CMD, and the chat-ws args). */}}
{{- define "cogo.port" -}}8000{{- end -}}

{{/* ------------------------------------------------------------------ labels */}}

{{- define "cogo.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "cogo.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: cogo
{{- end -}}

{{/* (dict "root" $ "component" "auth-api") */}}
{{- define "cogo.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cogo.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/* (dict "root" $ "component" "auth-api" "role" "api" "service" "auth") */}}
{{- define "cogo.componentLabels" -}}
{{ include "cogo.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- with .role }}
cogo.app/role: {{ . }}
{{- end }}
{{- with .service }}
cogo.app/service: {{ . }}
{{- end }}
{{- end -}}

{{/* Labels that select every CoGo pod of this release (network policies). */}}
{{- define "cogo.releaseSelector" -}}
app.kubernetes.io/name: {{ include "cogo.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* ------------------------------------------------------------------ values */}}

{{/*
Deep merge that, unlike sprig's merge/mergeOverwrite, lets `false`, 0 and "" in src win.
(dict "dst" <defaults> "src" <overrides>) -> YAML
*/}}
{{- define "cogo.deepMerge" -}}
{{- $dst := deepCopy (.dst | default dict) -}}
{{- range $k, $v := (.src | default dict) -}}
{{- $cur := get $dst $k -}}
{{- if and (kindIs "map" $v) (kindIs "map" $cur) -}}
{{- $_ := set $dst $k (include "cogo.deepMerge" (dict "dst" $cur "src" $v) | fromYaml) -}}
{{- else -}}
{{- $_ := set $dst $k $v -}}
{{- end -}}
{{- end -}}
{{- toYaml $dst -}}
{{- end -}}

{{/*
Every enabled deployable, with its role defaults merged in, as a YAML list of
{name, svcKey, svc, cfg}. Templates range over `include "cogo.deployables" . | fromYamlArray`.
*/}}
{{- define "cogo.deployables" -}}
{{- $out := list -}}
{{- $roles := list "gateway" "api" "ws" "worker" "scheduler" -}}
{{- range $svcKey, $svc := .Values.services -}}
{{- if dig "enabled" true $svc -}}
{{- range $name, $d := ($svc.deployables | default dict) -}}
{{- if dig "enabled" true $d -}}
{{- $role := required (printf "services.%s.deployables.%s.role is required" $svcKey $name) $d.role -}}
{{- if not (has $role $roles) -}}
{{- fail (printf "services.%s.deployables.%s.role %q: use one of %s" $svcKey $name $role (join ", " $roles)) -}}
{{- end -}}
{{- $cfg := include "cogo.deepMerge" (dict "dst" (index $.Values.roles $role) "src" $d) | fromYaml -}}
{{- $out = append $out (dict "name" $name "svcKey" $svcKey "svc" $svc "cfg" $cfg) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- toYaml $out -}}
{{- end -}}

{{/* Names of every enabled deployable, comma-separated (for target checks). */}}
{{- define "cogo.deployableNames" -}}
{{- $names := list -}}
{{- range $dp := include "cogo.deployables" . | fromYamlArray -}}
{{- $names = append $names $dp.name -}}
{{- end -}}
{{- join "," $names -}}
{{- end -}}

{{/* (dict "root" $ "cfg" $cfg) -> whether KEDA scales it */}}
{{- define "cogo.usesKeda" -}}
{{- if and .root.Values.keda.enabled (eq .cfg.role "worker") (dig "keda" "enabled" false .cfg) -}}true{{- end -}}
{{- end -}}

{{/* (dict "root" $ "cfg" $cfg) -> whether a CPU HPA scales it */}}
{{- define "cogo.usesHpa" -}}
{{- if and (has .cfg.role (list "gateway" "api" "ws")) (dig "autoscaling" "enabled" false .cfg) -}}true{{- end -}}
{{- end -}}

{{/* (dict "root" $ "cfg" $cfg) -> the lowest replica count it can run at */}}
{{- define "cogo.minReplicas" -}}
{{- if include "cogo.usesKeda" . -}}
{{- .cfg.keda.minReplicas | int -}}
{{- else if include "cogo.usesHpa" . -}}
{{- .cfg.autoscaling.minReplicas | int -}}
{{- else -}}
{{- .cfg.replicas | default 1 | int -}}
{{- end -}}
{{- end -}}

{{/* Deployables with a ClusterIP Service: gateway, APIs, chat-ws. Workers only for scraping. */}}
{{- define "cogo.isRoutable" -}}
{{- if has . (list "gateway" "api" "ws") -}}true{{- end -}}
{{- end -}}

{{/* ------------------------------------------------------------------ images */}}

{{/* (dict "root" $ "svc" $svc) */}}
{{- define "cogo.image" -}}
{{- $registry := .root.Values.image.registry -}}
{{- $tag := (.svc.image.tag | default .root.Values.image.tag) | default .root.Chart.AppVersion -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" (trimSuffix "/" $registry) .svc.image.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .svc.image.repository $tag -}}
{{- end -}}
{{- end -}}

{{/* ------------------------------------------------------------------ env */}}

{{/* One env value as a string: lists/maps as JSON, whole numbers without an exponent. */}}
{{- define "cogo.envValue" -}}
{{- if or (kindIs "slice" .) (kindIs "map" .) -}}
{{- toJson . -}}
{{- else if kindIs "float64" . -}}
{{- if eq . (floor .) -}}{{- int64 . -}}{{- else -}}{{- . -}}{{- end -}}
{{- else -}}
{{- toString . -}}
{{- end -}}
{{- end -}}

{{/* A map of env vars as an EnvVar list; null and "" are left out (service default). */}}
{{- define "cogo.envMap" -}}
{{- range $k, $v := . }}
{{- if not (or (kindIs "invalid" $v) (and (kindIs "string" $v) (eq $v ""))) }}
- name: {{ $k }}
  value: {{ include "cogo.envValue" $v | quote }}
{{- end }}
{{- end }}
{{- end -}}

{{/* (dict "root" $ "env" "REDIS_URL" "role" "auth") */}}
{{- define "cogo.redisEnv" -}}
- name: {{ .env }}
{{- if .root.Values.redis.existingSecret }}
  valueFrom:
    secretKeyRef:
      name: {{ .root.Values.redis.existingSecret }}
      key: {{ .role }}
{{- else }}
  value: {{ required (printf "redis.urls.%s is required" .role) (index .root.Values.redis.urls .role) | quote }}
{{- end }}
{{- end -}}

{{/*
In-cluster URL of a deployable's Service: (dict "root" $ "target" "auth-api" | "ws://chat-ws")
*/}}
{{- define "cogo.internalUrl" -}}
{{- $scheme := "http" -}}
{{- $target := .target -}}
{{- if contains "://" .target -}}
{{- $parts := splitList "://" .target -}}
{{- $scheme = first $parts -}}
{{- $target = last $parts -}}
{{- end -}}
{{- if not (has $target (splitList "," (include "cogo.deployableNames" .root))) -}}
{{- fail (printf "internal URL target %q is not an enabled deployable" $target) -}}
{{- end -}}
{{- printf "%s://%s:%s" $scheme (include "cogo.componentName" (dict "root" .root "name" $target)) (include "cogo.port" .root) -}}
{{- end -}}

{{/*
DATABASE_URL for a service: (dict "root" $ "db" $svc.database "direct" true|false).
The password is expanded by Kubernetes from the <passwordKey> env var defined before it.
*/}}
{{- define "cogo.databaseUrl" -}}
{{- $pg := .root.Values.postgres -}}
{{- $host := required "postgres.host is required" $pg.host -}}
{{- $port := $pg.port -}}
{{- $query := $pg.urlQuery -}}
{{- if and .root.Values.pgbouncer.enabled (not .direct) -}}
{{- $host = include "cogo.componentName" (dict "root" .root "name" "pgbouncer") -}}
{{- $port = .root.Values.pgbouncer.port -}}
{{- $query = .root.Values.pgbouncer.appUrlQuery -}}
{{- end -}}
{{- printf "postgresql+asyncpg://%s:$(%s)@%s:%d/%s" .db.user .db.passwordKey $host (int $port) .db.name -}}
{{- with $query -}}?{{ . }}{{- end -}}
{{- end -}}

{{/* The DB password env var (must come before DATABASE_URL). (dict "svc" $svc) */}}
{{- define "cogo.dbPasswordEnv" -}}
- name: {{ .svc.database.passwordKey }}
  valueFrom:
    secretKeyRef:
      name: {{ required "services.<name>.secretName is required with a database" .svc.secretName }}
      key: {{ .svc.database.passwordKey }}
{{- end -}}
