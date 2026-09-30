"""Static checks of the Terraform files and the deploy workflow.

No `terraform validate` runs here (no Terraform binary, no AWS credentials, in this environment) -
these are lightweight, purpose-built regex checks over the .tf text, plus a couple of checks that
tie the infrastructure directly to the Python code (env var names, IAM actions) so the two cannot
silently drift apart. They catch typos and dangling references; they are not a substitute for a
real `terraform validate`/`plan` before deploying.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF_DIR = ROOT / "terraform"
TF_FILES = sorted(TF_DIR.glob("*.tf"))

RESOURCE_RE = re.compile(r'resource\s+"([a-z0-9_]+)"\s+"([a-zA-Z0-9_]+)"\s*\{')
DATA_RE = re.compile(r'data\s+"([a-z0-9_]+)"\s+"([a-zA-Z0-9_]+)"\s*\{')
VARIABLE_RE = re.compile(r'variable\s+"([a-zA-Z0-9_]+)"\s*\{')
OUTPUT_RE = re.compile(r'output\s+"([a-zA-Z0-9_]+)"\s*\{')
# references like aws_dynamodb_table.blocks.arn / var.foo / data.aws_iam_policy_document.x.json
REF_RE = re.compile(r'\b(aws_[a-z0-9_]+)\.([a-zA-Z0-9_]+)\b')
VAR_REF_RE = re.compile(r'\bvar\.([a-zA-Z0-9_]+)\b')
DATA_REF_RE = re.compile(r'\bdata\.([a-z0-9_]+)\.([a-zA-Z0-9_]+)\b')


def _strip_comments(text: str) -> str:
    text = re.sub(r'#.*', '', text)
    text = re.sub(r'//.*', '', text)
    return re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)


class TerraformStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = {f.name: f.read_text() for f in TF_FILES}
        cls.raw["terraform.tfvars.example"] = (TF_DIR / "terraform.tfvars.example").read_text()
        cls.text = {name: _strip_comments(t) for name, t in cls.raw.items()}
        cls.all_text = "\n".join(cls.text.values())
        cls.resources = {(m.group(1), m.group(2)) for t in cls.text.values() for m in RESOURCE_RE.finditer(t)}
        cls.data_sources = {(m.group(1), m.group(2)) for t in cls.text.values() for m in DATA_RE.finditer(t)}
        cls.variables = {m.group(1) for m in VARIABLE_RE.finditer(cls.text["variables.tf"])}
        cls.outputs = {m.group(1) for m in OUTPUT_RE.finditer(cls.text["outputs.tf"])}

    # -- syntactic sanity ------------------------------------------------------------------
    def test_every_tf_file_has_balanced_braces_and_parens(self):
        for name, t in self.text.items():
            with self.subTest(file=name):
                self.assertEqual(t.count("{"), t.count("}"))
                self.assertEqual(t.count("("), t.count(")"))
                self.assertEqual(t.count('"""'), 0)   # no accidental triple-quoted leftovers

    def test_no_file_is_suspiciously_empty(self):
        for f in TF_FILES:
            with self.subTest(file=f.name):
                self.assertGreater(len(self.text[f.name].strip()), 20)

    def test_resource_and_variable_names_are_unique(self):
        resource_list = [(m.group(1), m.group(2)) for t in self.text.values() for m in RESOURCE_RE.finditer(t)]
        self.assertEqual(len(resource_list), len(set(resource_list)))
        variable_list = VARIABLE_RE.findall(self.text["variables.tf"])
        self.assertEqual(len(variable_list), len(set(variable_list)))

    # -- referential integrity: nothing points at a resource/variable/data source that doesn't exist
    def test_every_aws_resource_reference_resolves_to_a_declared_resource(self):
        # exclude the LHS of resource/data declarations themselves
        declared_lines = set()
        for t in self.text.values():
            declared_lines |= {m.group(0) for m in RESOURCE_RE.finditer(t)} | {m.group(0) for m in DATA_RE.finditer(t)}
        body = self.all_text
        for decl in declared_lines:
            body = body.replace(decl, "", 1)
        missing = []
        for m in REF_RE.finditer(body):
            ref = (m.group(1), m.group(2))
            if ref not in self.resources and ref not in {("aws_iam_policy_document", n) for _, n in self.data_sources}:
                # could legitimately be a data source reference (aws_iam_policy_document.x) or a resource
                if ("data", ref) not in [(k[0], (k[0], k[1])) for k in self.data_sources]:
                    if ref not in self.resources:
                        missing.append(ref)
        # aws_iam_policy_document references are data sources, not resources - check those separately
        missing = [r for r in missing if not any(r[1] == name for _, name in self.data_sources)]
        self.assertEqual(missing, [], f"references to undeclared resources: {missing}")

    def test_every_var_reference_is_a_declared_variable(self):
        used = {m.group(1) for m in VAR_REF_RE.finditer(self.all_text)}
        self.assertEqual(used - self.variables, set())

    def test_every_variable_is_referenced_at_least_once(self):
        used = {m.group(1) for m in VAR_REF_RE.finditer(self.all_text)}
        # every declared variable should be used somewhere (dead config is a smell)
        self.assertEqual(self.variables - used, set())

    def test_every_data_reference_matches_a_declared_data_source(self):
        for m in DATA_REF_RE.finditer(self.all_text):
            kind, name = m.group(1), m.group(2)
            self.assertIn((kind, name), self.data_sources, f"data.{kind}.{name} is not declared anywhere")

    # -- structural expectations of this specific stack ------------------------------------
    def test_outputs_reference_only_declared_resources(self):
        for m in REF_RE.finditer(self.text["outputs.tf"]):
            self.assertIn((m.group(1), m.group(2)), self.resources)

    def test_expected_core_resources_exist(self):
        expected = {
            ("aws_dynamodb_table", "blocks"), ("aws_sns_topic", "alerts"),
            ("aws_iam_role", "lambda"), ("aws_iam_role_policy", "lambda"),
            ("aws_lambda_function", "api"), ("aws_apigatewayv2_api", "this"),
            ("aws_apigatewayv2_stage", "default"), ("aws_lambda_permission", "apigw"),
            ("aws_cloudwatch_metric_alarm", "blocking_a_lot"), ("aws_cloudwatch_metric_alarm", "lambda_errors"),
        }
        self.assertTrue(expected <= self.resources, expected - self.resources)

    def test_dynamodb_table_has_a_ttl_attribute_matching_the_python_store(self):
        # immune/store.py's DynamoBlockStore writes 'expires_at' as the TTL field
        m = re.search(r'resource\s+"aws_dynamodb_table"\s+"blocks"\s*\{(.*?)\n\}', self.text["main.tf"], re.DOTALL)
        self.assertIsNotNone(m)
        body = m.group(1)
        ttl = re.search(r'ttl\s*\{([^}]*)\}', body, re.DOTALL)
        self.assertIsNotNone(ttl, "no ttl block on the blocks table")
        self.assertIn('attribute_name = "expires_at"', ttl.group(1))
        self.assertIn("enabled        = true", ttl.group(1))
        self.assertEqual(re.search(r'hash_key\s*=\s*"([^"]+)"', body).group(1), "pk")   # matches store.py's Key={"pk": ...}

    def test_iam_policy_grants_exactly_the_actions_the_guard_needs(self):
        # cross-check against what immune/store.py and immune/guard.py actually call
        store_src = (ROOT / "immune" / "store.py").read_text()
        guard_src = (ROOT / "immune" / "guard.py").read_text()
        m = re.search(r'data\s+"aws_iam_policy_document"\s+"lambda_permissions"\s*\{(.*?)\n\}\n', self.text["main.tf"], re.DOTALL)
        self.assertIsNotNone(m)
        actions = set(re.findall(r'"([a-z0-9]+:[A-Za-z]+)"', m.group(1)))
        self.assertIn("dynamodb:GetItem", actions)
        self.assertIn("dynamodb:PutItem", actions)
        self.assertIn("sns:Publish", actions)
        # the code only ever calls get_item/put_item on Dynamo and publish on SNS - nothing broader
        self.assertNotIn("dynamodb:Scan", actions)
        self.assertNotIn("dynamodb:DeleteItem", actions)
        self.assertNotIn("sns:*", actions)
        self.assertIn("client.get_item", store_src)
        self.assertIn("client.put_item", store_src)
        self.assertIn("sns.publish", guard_src)
        # no other AWS service should appear in the policy at all (least privilege)
        services = {a.split(":")[0] for a in actions}
        self.assertEqual(services, {"logs", "dynamodb", "sns"})

    def test_lambda_env_vars_match_guardconfig_from_env(self):
        # every IMMUNE_* var GuardConfig.from_env() reads should be set by Terraform (or safely defaulted)
        guard_src = (ROOT / "immune" / "guard.py").read_text()
        expected = set(re.findall(r'env\.get\("(IMMUNE_[A-Z_]+)"', guard_src))
        m = re.search(r'environment\s*\{\s*variables\s*=\s*\{(.*?)\}\s*\}', self.text["main.tf"], re.DOTALL)
        self.assertIsNotNone(m)
        configured = set(re.findall(r'(IMMUNE_[A-Z_]+)\s*=', m.group(1)))
        # IMMUNE_MODEL_PATH/IMMUNE_TABLE/IMMUNE_SNS_TOPIC_ARN are read directly (not via from_env),
        # so they don't have to appear in `expected`, but everything from_env() reads should be set.
        self.assertTrue(expected <= configured, f"GuardConfig reads {expected - configured} but Terraform never sets it")

    def test_apigateway_has_a_throttle_independent_of_the_guard(self):
        m = re.search(r'default_route_settings\s*\{([^}]*)\}', self.text["main.tf"], re.DOTALL)
        self.assertIsNotNone(m)
        self.assertIn("throttling_burst_limit", m.group(1))
        self.assertIn("throttling_rate_limit", m.group(1))

    def test_guard_mode_variable_is_constrained_to_known_values(self):
        m = re.search(r'variable\s+"guard_mode"\s*\{(.*?)\n\}', self.text["variables.tf"], re.DOTALL)
        self.assertIsNotNone(m)
        self.assertIn('contains(["enforce", "monitor"]', m.group(1))

    def test_alarms_reference_the_metric_namespace_the_guard_actually_emits(self):
        # immune/guard.py's Guard._alert() uses namespace=self.cfg.namespace, default "ImmuneGuard"
        guard_src = (ROOT / "immune" / "guard.py").read_text()
        self.assertIn('namespace: str = "ImmuneGuard"', guard_src)
        self.assertIn('namespace           = "ImmuneGuard"', self.text["main.tf"])

    def test_backend_is_remote_s3_not_local_state(self):
        self.assertIn('backend "s3"', self.text["backend.tf"])

    def test_tfvars_example_has_no_real_looking_secrets(self):
        content = self.raw["terraform.tfvars.example"]
        self.assertNotRegex(content, r'AKIA[0-9A-Z]{16}')       # an actual-looking AWS access key
        m = re.search(r'alert_email\s*=\s*"([^"]*)"', content)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "")                        # the assigned value is blank, not a real address


class DeployWorkflowTests(unittest.TestCase):
    """The GitHub Actions workflow that trains, evaluates, gates and deploys."""

    @classmethod
    def setUpClass(cls):
        import yaml
        cls.yaml = yaml
        cls.deploy = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy.yml").read_text())
        cls.ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())

    def test_both_workflow_files_are_valid_yaml_with_jobs(self):
        self.assertIn("jobs", self.deploy)
        self.assertIn("jobs", self.ci)

    def test_deploy_runs_tests_before_training_before_evaluating_before_gating_before_packaging(self):
        steps = [s.get("run", "") for s in self.deploy["jobs"]["train-and-evaluate"]["steps"] if "run" in s]
        order = ["unittest discover", "train_model.sh", "evaluate_model.sh", "check_thresholds.py", "build_lambda.sh"]
        positions = [next(i for i, s in enumerate(steps) if key in s) for key in order]
        self.assertEqual(positions, sorted(positions), "pipeline steps are out of order")

    def test_deploy_job_depends_on_the_evaluation_gate_and_only_runs_on_a_real_push(self):
        deploy_job = self.deploy["jobs"]["deploy"]
        self.assertEqual(deploy_job["needs"], "train-and-evaluate")
        # never triggered by pull_request (would run untrusted code with real AWS credentials)
        self.assertNotIn("pull_request", self.deploy.get(True, self.deploy.get("on", {})))

    def test_deploy_uses_oidc_role_assumption_not_static_credentials(self):
        creds_step = next(s for s in self.deploy["jobs"]["deploy"]["steps"] if "aws-actions/configure-aws-credentials" in s.get("uses", ""))
        self.assertIn("role-to-assume", creds_step["with"])
        self.assertNotIn("aws-access-key-id", creds_step.get("with", {}))

    def test_lambda_zip_path_passed_to_terraform_matches_the_build_script_output(self):
        plan_step = next(s for s in self.deploy["jobs"]["deploy"]["steps"] if "terraform plan" in s.get("run", ""))
        build_script = (ROOT / "scripts" / "build_lambda.sh").read_text()
        self.assertIn("build/app.zip", plan_step["run"])
        self.assertIn('OUT="$BUILD/app.zip"', build_script)

    def test_concurrency_group_prevents_overlapping_applies(self):
        self.assertIn("concurrency", self.deploy)
        self.assertFalse(self.deploy["concurrency"].get("cancel-in-progress", True))


if __name__ == "__main__":
    unittest.main()
