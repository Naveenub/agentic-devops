def parse_error(log: str):
    if "AccessDenied" in log:
        return "IAM permission missing for Terraform role"
