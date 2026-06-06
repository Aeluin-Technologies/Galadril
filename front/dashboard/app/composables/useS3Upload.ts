export interface IamPermission {
  effect: "allow" | "deny";
  action: string;
  scope: {
    principal?: string;
    role?: string;
    [key: string]: any;
  };
}

export function useS3Upload() {
  const { t } = useI18n();
  const isUploading = ref(false);
  const error = ref<string | null>(null);

  async function requestStagingUpload(
    fileName: string,
  ): Promise<{ uploadUrl: string; stagingKey: string } | null> {
    error.value = null;
    try {
      const query = `
        mutation RequestStaging($fileName: String!) {
          requestStagingUpload(fileName: $fileName) {
            uploadUrl
            stagingKey
          }
        }
      `;
      const response = await $fetch<{ data: any; errors?: any[] }>(
        "/api/graphql",
        {
          method: "POST",
          body: { query, variables: { fileName } },
        },
      );

      if (response.errors?.length) {
        throw new Error(response.errors[0].message);
      }
      return response.data.requestStagingUpload;
    } catch (e: any) {
      error.value = e.message || t("storage.upload.default_error");
      return null;
    }
  }

  async function uploadToS3Presigned(
    url: string,
    file: File,
  ): Promise<boolean> {
    try {
      const response = await fetch(url, {
        method: "PUT",
        body: file,
        headers: {
          "Content-Type": file.type,
        },
      });
      return response.ok;
    } catch (e) {
      error.value = t("storage.upload.s3_error");
      return false;
    }
  }

  async function completeUpload(
    stagingKey: string,
    targetName: string,
    permissions: IamPermission[],
  ): Promise<string | null> {
    try {
      const query = `
        mutation CompleteUpload($stagingKey: String!, $targetName: String!, $permissionsJson: String) {
          completeUpload(stagingKey: $stagingKey, targetName: $targetName, permissionsJson: $permissionsJson)
        }
      `;
      const permissionsJson =
        permissions.length > 0 ? JSON.stringify(permissions) : null;

      const response = await $fetch<{ data: any; errors?: any[] }>(
        "/api/graphql",
        {
          method: "POST",
          body: {
            query,
            variables: { stagingKey, targetName, permissionsJson },
          },
        },
      );

      if (response.errors?.length) {
        throw new Error(response.errors[0].message);
      }
      return response.data.completeUpload;
    } catch (e: any) {
      error.value = e.message || t("storage.upload.complete_error");
      return null;
    }
  }

  return {
    isUploading,
    error,
    requestStagingUpload,
    uploadToS3Presigned,
    completeUpload,
  };
}
