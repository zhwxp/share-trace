/** 构建期的资源版本号：/assets/*.js 走 7 天缓存又没有内容哈希，加个 ?v= 让改动能立刻生效 */
export const V = Date.now().toString(36);
