import { DeploymentWorkflow } from './AgenticDeploymentWorkspace';

export default function ManualDeploymentWorkspace(props) {
    return <DeploymentWorkflow {...props} mode="manual" />;
}
