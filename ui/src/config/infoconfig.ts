export interface InfobarContent {
  title: string;
  content: string[];
  learnMore?: { text: string; url: string };
}

export const workspacesInfoContent: InfobarContent = {
  title: 'AutoLab',
  content: ['自动化科研训练台 · M1 单机纵切（详见 docs/）']
};
