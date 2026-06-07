use anyhow::Result;
use mistralrs::tool;

/// A simple calculator tool for the model to compute facts if needed.
#[tool(
    description = "Evaluate a mathematical expression (e.g., '2 + 2 * 4', 'sin(pi/2)')."
)]
pub fn calculator(
    #[description = "The mathematical expression to evaluate."]
    expression: String,
) -> Result<String> {
    tracing::debug!(?expression, "calculator tool invoked");

    let clean_expr = expression.trim();
    if clean_expr.is_empty() {
        return Ok("Error: Expression is empty.".to_string());
    }

    match meval::eval_str(clean_expr) {
        Ok(result) => Ok(format!("Result: {result}")),
        Err(e) => Ok(format!("Error evaluating expression: {e}")),
    }
}
