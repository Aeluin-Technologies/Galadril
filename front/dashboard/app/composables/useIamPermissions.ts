export interface IamPermission {
  effect: "allow" | "deny";
  action: string;
  scope: {
    principal?: string;
    role?: string;
    [key: string]: any;
  };
}

interface SetPermissionsResponse {
  setUserPermissions: boolean;
  setRolePermissions: boolean;
}

export function useIamPermissions() {
  const { t } = useI18n();
  const permissionsList = ref<IamPermission[]>([]);
  const isProcessing = ref(false);
  const permissionError = ref<string | null>(null);

  const SET_USER_PERMISSIONS_MUTATION = gql`
    mutation SetUserPermissions(
      $userId: String!
      $permissions: [GqlPermissionInput!]!
    ) {
      setUserPermissions(userId: $userId, permissions: $permissions)
    }
  `;

  const SET_ROLE_PERMISSIONS_MUTATION = gql`
    mutation SetRolePermissions(
      $roleName: String!
      $permissions: [GqlPermissionInput!]!
    ) {
      setRolePermissions(roleName: $roleName, permissions: $permissions)
    }
  `;

  const { mutate: setUserPermissionsMutate } =
    useMutation<SetPermissionsResponse>(SET_USER_PERMISSIONS_MUTATION);
  const { mutate: setRolePermissionsMutate } =
    useMutation<SetPermissionsResponse>(SET_ROLE_PERMISSIONS_MUTATION);

  /**
   * Validates if the permission configuration conforms to SpiceDB naming constraints.
   * @param {IamPermission} permission - The individual permission unit descriptor.
   * @returns {boolean} True if syntax complies with standard object/relation semantics.
   */
  const validateSpiceDbSyntax = (permission: IamPermission): boolean => {
    const spiceDbIdRegex = /^[a-zA-Z0-9_|\-]+$/;
    const targetId = permission.scope.principal || permission.scope.role;

    if (!targetId || !spiceDbIdRegex.test(targetId)) return false;
    if (!permission.action || !spiceDbIdRegex.test(permission.action))
      return false;
    if (!["allow", "deny"].includes(permission.effect)) return false;

    return true;
  };

  /**
   * appends a valid permission descriptor to the internal reactive list.
   * @param {IamPermission} permission - The candidate authorization object rule.
   * @returns {boolean} True if the permission passed validation and was added.
   */
  const addPermission = (permission: IamPermission): boolean => {
    permissionError.value = null;

    if (!validateSpiceDbSyntax(permission)) {
      permissionError.value = t("storage.permissions.syntax_error");
      return false;
    }

    const isDuplicate = permissionsList.value.some(
      (p) =>
        p.effect === permission.effect &&
        p.action === permission.action &&
        p.scope.role === permission.scope.role &&
        p.scope.principal === permission.scope.principal,
    );

    if (!isDuplicate) {
      permissionsList.value.push(permission);
      return true;
    }

    return false;
  };

  /**
   * Evicts a permission wrapper from the tracking state array.
   * @param {number} index - The targeted index layout reference.
   * @returns {void}
   */
  const removePermission = (index: number): void => {
    permissionsList.value.splice(index, 1);
  };

  /**
   * Flushes and resets the internal tracking permission list stack.
   * @returns {void}
   */
  const clearPermissions = (): void => {
    permissionsList.value = [];
    permissionError.value = null;
  };

  /**
   * Dispatches and persists permissions for an explicit user identifier.
   * @param {string} userId - Target unique identity specifier.
   * @returns {Promise<boolean>}
   */
  const saveUserPermissions = async (userId: string): Promise<boolean> => {
    isProcessing.value = true;
    permissionError.value = null;
    try {
      const response = await setUserPermissionsMutate({
        userId,
        permissions: permissionsList.value,
      });
      return !!response?.data?.setUserPermissions;
    } catch (e: any) {
      permissionError.value =
        e.message || t("storage.permissions.update_error");
      return false;
    } finally {
      isProcessing.value = false;
    }
  };

  /**
   * Dispatches and persists permissions for an explicit tenant role bucket.
   * @param {string} roleName - Target unique security group token descriptor.
   * @returns {Promise<boolean>}
   */
  const saveRolePermissions = async (roleName: string): Promise<boolean> => {
    isProcessing.value = true;
    permissionError.value = null;
    try {
      const response = await setRolePermissionsMutate({
        roleName,
        permissions: permissionsList.value,
      });
      return !!response?.data?.setRolePermissions;
    } catch (e: any) {
      permissionError.value =
        e.message || t("storage.permissions.update_error");
      return false;
    } finally {
      isProcessing.value = false;
    }
  };

  return {
    permissionsList,
    isProcessing,
    permissionError,
    addPermission,
    removePermission,
    clearPermissions,
    validateSpiceDbSyntax,
    saveUserPermissions,
    saveRolePermissions,
  };
}
