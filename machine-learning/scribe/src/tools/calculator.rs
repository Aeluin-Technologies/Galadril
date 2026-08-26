use anyhow::Result;
use mistralrs::tool;

/// A simple calculator tool for the model to compute facts if needed.
#[tool(
    description = "Evaluate a mathematical expression (e.g., '2 + 2 * 4', 'sin(pi/2)')."
)]
#[tracing::instrument(name = "scribe.tool.calculator", skip(expression))]
pub fn calculator(
    #[description = "The mathematical expression to evaluate."]
    expression: String,
) -> Result<String> {
    tracing::debug!(
        event.name = "scribe.tool.calculator.started",
        ?expression,
        "calculator tool started"
    );

    let clean_expr = expression.trim();
    if clean_expr.is_empty() {
        return Ok("Error: Expression is empty.".to_string());
    }

    let response = match meval::eval_str(clean_expr) {
        Ok(result) => format!("Result: {result}"),
        Err(error) => format!("Error evaluating expression: {error}"),
    };
    tracing::debug!(
        event.name = "scribe.tool.calculator.completed",
        "calculator tool completed"
    );
    Ok(response)
}

#[cfg(test)]
#[cfg_attr(coverage, coverage(off))]
mod tests {
    use super::*;

    #[test]
    fn test_calculator_empty() -> Result<()> {
        let res = calculator("   ".to_string())?;
        assert_eq!(res, "Error: Expression is empty.");
        Ok(())
    }

    #[test]
    fn test_calculator_success() -> Result<()> {
        let res = calculator("2 + 2 * 4".to_string())?;
        assert_eq!(res, "Result: 10");
        Ok(())
    }

    #[test]
    fn test_calculator_invalid() -> Result<()> {
        let res = calculator("invalid(expr".to_string())?;
        assert!(res.contains("Error evaluating expression"));
        Ok(())
    }
}
