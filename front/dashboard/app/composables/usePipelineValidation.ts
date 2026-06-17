import { ref } from "vue";

export const usePipelineValidation = () => {
  const errors = ref<Record<string, string>>({});

  const validateRegex = (key: string, pattern: string): boolean => {
    if (!pattern) {
      errors.value[key] = "errors.required";
      return false;
    }
    if (pattern.length > 100) {
      errors.value[key] = "errors.tooLong";
      return false;
    }
    try {
      new RegExp(pattern);
      delete errors.value[key];
      return true;
    } catch (e) {
      errors.value[key] = "errors.invalidRegex";
      return false;
    }
  };

  const validateThreshold = (key: string, val: number): boolean => {
    if (val === undefined || val === null || isNaN(val)) {
      errors.value[key] = "errors.required";
      return false;
    }
    if (val < 0 || val > 1) {
      errors.value[key] = "errors.invalidRange";
      return false;
    }
    delete errors.value[key];
    return true;
  };

  const validateText = (key: string, text: string, maxLen = 100): boolean => {
    if (!text || !text.trim()) {
      errors.value[key] = "errors.required";
      return false;
    }
    if (text.length > maxLen) {
      errors.value[key] = "errors.tooLong";
      return false;
    }
    delete errors.value[key];
    return true;
  };

  return { errors, validateRegex, validateThreshold, validateText };
};
